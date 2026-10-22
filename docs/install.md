# Install

To get usd_chunks working, install the package into the Python your host runs. It comes
with a set of chunks, so that's all you need to start. Set **USD_CHUNK_DIRS** later, when
you write chunks of your own.

This page covers usdview and plain Python. **Houdini** and **Blender** install differently:
see [Houdini](houdini.md) and [Blender](blender.md).

## Quick start

You need **Python 3.11+**, and it has to be the same Python your USD was built with (see
[Requirements](#requirements)). On Windows (cmd), where `python` is usdview's own Python:

```
git clone https://github.com/HuginStudios/usd_chunks.git
python -m pip install -e ./usd_chunks
for /f "delims=" %i in ('usdchunks plugin-path') do set PXR_PLUGINPATH_NAME=%i
usdview anything.usda
```

If `pip install -e` fails, your pip is probably too old for editable installs. Run
`python -m pip install --upgrade pip` and try again.

If pip fails with `CERTIFICATE_VERIFY_FAILED`, your network checks HTTPS traffic and that
pip is too old to use Windows' own certificates. Pip 24.2 and newer use them. Download a
new pip with any Python that works (`python -m pip download pip --no-deps -d wheels`), then
install that file with the old one (`python -m pip install wheels\pip-<version>.whl`).

To run without usdview, using `usd-core` only:

```
git clone https://github.com/HuginStudios/usd_chunks.git
python -m pip install -e "./usd_chunks[standalone]"
usdchunks run --help
```

For something to run, the [Scripting](scripting.md#in-a-terminal) page builds and saves a
small network.

In PowerShell, use `$env:PXR_PLUGINPATH_NAME = (usdchunks plugin-path)` and
`$env:NAME = "value"` for other variables. On macOS/Linux, use
`export PXR_PLUGINPATH_NAME="$(usdchunks plugin-path)"` and `export NAME=value`.

In usdview, right-click the graph canvas (or press Tab) to see the chunks in
[`chunks/`](https://github.com/HuginStudios/usd_chunks/tree/main/chunks). When you start
writing your own chunks, point `USD_CHUNK_DIRS` at your own folder. You only need the rest
of this page if something goes wrong or you want more detail.

No **Chunks** panel when usdview opens? Usually usd_chunks was installed into a different
Python than the one usdview uses. See [If the Chunks panel is
missing](#if-the-chunks-panel-is-missing).

## Install

### Requirements

- **Python 3.11 or newer.** usd_chunks uses `typing.dataclass_transform`, which was added
  in 3.11.
- **The same Python your USD was built with.** USD's Python bindings only work with the
  Python version they were compiled for, so usd_chunks has to be installed into that
  Python: usdview's, Maya's `mayapy`, and so on. If your USD was built with a Python older
  than 3.11, you need to rebuild USD with 3.11 or newer first. Houdini and Blender have
  their own setup: see [Houdini](houdini.md) and [Blender](blender.md).

### Getting the package

usd_chunks isn't on PyPI yet, so start by cloning it:

```
git clone https://github.com/HuginStudios/usd_chunks.git
```

How you install it depends on whether the Python you're installing into already has USD.
Check with:

```
python -c "import pxr"
```

**If that works**, you're in usdview's Python, Maya's `mayapy` or another app that has
USD. Install it as is:

```
python -m pip install -e ./usd_chunks
```

**If that fails**, you're in a plain Python (a script, a CI job, a new venv) and need USD
as well:

```
python -m pip install -e "./usd_chunks[standalone]"
```

The `standalone` extra installs `usd-core`, so you get the library and the `usdchunks`
command without building USD yourself. `usdchunks run my_setup.usda` works straight away.

!!! danger "🚫 Never install `[standalone]` into a host that already has USD"
    Houdini, Maya, Blender and usdview each ship their own USD build. Blender's, for
    example, is compiled as `pxrBlender_v26_03__pxrReserved__`. A second USD installed with
    pip is a completely separate library that can't share objects with the host's. You end
    up with two USDs in one process, and things break in confusing ways.

The `usd-core` package on PyPI doesn't include usdview. To use usd_chunks in usdview you
need a full USD build that includes `pxr.Usdviewq`. Build one with OpenUSD's
`build_scripts/build_usd.py`, or use a prebuilt one, such as NVIDIA's. See [usdview
integration](#usdview-integration) below.

### ⚠️ A plain install won't show every bundled chunk

!!! warning
    A plain install only includes attrs. About one in six of the chunks in
    [`chunks/`](https://github.com/HuginStudios/usd_chunks/tree/main/chunks) need another
    package, mostly numpy.

If a chunk module fails to import, it's left out of the create menu. The console shows a
warning that names the missing package, then the import error. Nothing shows in the UI.
Usually the first thing you notice is that the chunk you want isn't in the menu.

The chunks affected today: `AMDMaterialXLibrary`, `add_curves`, `cad_import`,
`curves_to_mesh`, `custom_widget`, `mesh`, `poly_reduce`, `rbd_solver`, `requirements`
(an example), `scatter_brush`, `scatter_points`, `urdf_import`, `uv_checker` and `vine`.
That includes some of the main ones, like Mesh, Scatter, Poly Reduce and the RBD solver.

There are two ways to fix it:

- Install the packages yourself: `pip install numpy requests mujoco fast-simplification`.
  mujoco is only for the RBD solver, and fast-simplification only for Poly Reduce. CAD
  Import also needs `cadquery-ocp`. Add `PySide6` only if your host doesn't have Qt
  already; usdview, Houdini and Maya do. You can also install just the ones you need.
- Let usd_chunks ask. When you create a chunk whose `requirements` ([Writing a
  chunk](writing-a-chunk.md#config)) aren't installed, usdview asks "OK to install requests
  for chunk AMDMaterialXLibrary?" and installs them if you say yes, with a progress dialog.
  `usdchunks run` asks the same on an interactive terminal, and Blender in a popup.

### Other extras

These are only needed for specific work. You can combine them, for example
`pip install -e ".[dev,docs]"`.

| Extra | What it's for |
| --- | --- |
| `ui` | Installs PySide6, for a host that doesn't have Qt. usdview and Houdini already have Qt, so skip it there. |
| `flet` | Running the standalone Flet graph frontend (`python -m usd_chunks.frontends.flet.nodegraph <network.usda>`) instead of a Qt host. |
| `imgui` | The same for the Dear ImGui frontend (`python -m usd_chunks.frontends.imgui.nodegraph <network.usda>`). |
| `dev` | Working on usd_chunks itself: pytest, ruff, build tools, `usd-core`, and the Flet and ImGui frontends so the tests can cover them. |
| `docs` | Building the docs site. See [Build documentation](#build-documentation). |

## usdview integration

usdview loads usd_chunks as a USD plugin, so it needs to know where the plugin's
`plugInfo.json` is. You tell it with `PXR_PLUGINPATH_NAME`. This command prints the right
path for your install:

```
usdchunks plugin-path
```

Set `PXR_PLUGINPATH_NAME` to that path before starting usdview. With that and
`USD_CHUNK_DIRS` set, usdview opens with a **Chunks** panel and a **Chunks** menu.

## If the Chunks panel is missing

### ⚠️ Installed into the wrong Python

This is the most common cause, and there's no error message: the panel just isn't there.
Most machines have several Pythons installed, and usdview usually has its own, which is
often not the one plain `pip` installs into.

Find out which Python usdview uses (run `import sys; print(sys.executable)` in its Python
console, or look in your launcher script), then run pip with that Python:

```
"<that path>" -m pip install -e usd_chunks     # usdview
mayapy -m pip install -e usd_chunks            # Maya
```

Houdini and Blender don't use `pip install` like this. See [Houdini](houdini.md) and
[Blender](blender.md).

If you already installed into the wrong Python, reinstall with the right one. A regular
install can be fixed by pointing `PYTHONPATH` at that environment's `site-packages`, but an
editable (`-e`) install can't. Editable installs rely on a `.pth` file, and Python only
reads those from real site directories, not from `PYTHONPATH`.

### ⚠️ Testing the import from the folder you cloned into

If you run `python -c "..."` or a script from the folder that contains `usd_chunks/`,
Python imports that folder instead of your install. The error then names something odd,
like `ATTRS_VERSION`, instead of saying the module wasn't found. Change to any other folder
before testing with plain `python`. usdview and the `usdchunks` command aren't affected.

## If a chunk is missing from the menu

The panel is there, but a chunk like Scatter, Mesh or the RBD solver isn't in the
create menu. This is almost always a missing pip package: the chunk failed to import, so
it was left out. The console shows which package when it loads:

```
WARNING: usd_chunks.chunk_registry: Chunk scatter needs numpy>=2,<3, which is not installed. ...
```

Install that package into the Python usdview uses. The full list and the fix are under [A
plain install won't show every bundled
chunk](#a-plain-install-wont-show-every-bundled-chunk).

## Gizmos (viewport handles)

The handles you drag in the viewport to change a chunk's params are in a separate repo,
[usd_gizmos](https://github.com/HuginStudios/usd_gizmos). Once usd_chunks is set up, it
takes three more commands:

```
git clone https://github.com/HuginStudios/usd_gizmos.git
"<usdview's python>" -m pip install -e ./usd_gizmos
set PXR_PLUGINPATH_NAME=%PXR_PLUGINPATH_NAME%;C:\path\to\usd_gizmos\gizmo\plugins\usdview
```

PowerShell: `$env:PXR_PLUGINPATH_NAME += ";C:\path\to\usd_gizmos\gizmo\plugins\usdview"`.
macOS/Linux: `export PXR_PLUGINPATH_NAME="$PXR_PLUGINPATH_NAME:/path/to/usd_gizmos/gizmo/plugins/usdview"`.

As with usd_chunks, install it with usdview's own Python (see [Installed into the wrong
Python](#installed-into-the-wrong-python)). The path ends in `...\plugins\usdview`. The
folder next to it, `...\plugins\kraken`, is for Kraken and easy to pick by mistake.

**What you should see:** select any prim and a transform handle appears in the viewport.
When you drag it, a Transform chunk appears in the graph and stores the move. Dragging
never changes the prim directly. Camera and Scatter Brush chunks have their own handles.

### How the two repos split the work

usd_chunks only **describes** a handle. A chunk lists `gizmos` in its `config()` ([Writing
a chunk](writing-a-chunk.md#config)), which writes `ui:gizmo:*` attributes saying which
handle exists and which params it controls. usd_gizmos **draws** the handle and handles the
dragging. Each repo works without the other: without usd_gizmos you just don't see handles,
and without usd_chunks the handles don't control any chunk params. See [Chunk gizmos: the
host contract](gizmo-host-contract.md) if you're building handles for another host.

### If the handle is missing, or dragging doesn't change the params

- **No handle at all:** `PXR_PLUGINPATH_NAME` doesn't include
  `usd_gizmos\gizmo\plugins\usdview` (make sure you didn't pick the `kraken` folder), or
  usd_gizmos was installed into a different Python than usdview's.
- **A handle shows, but the chunk's params don't change:** an older gizmo plugin is loaded
  as well. Both replace the same viewport functions, and nothing on screen shows which one
  you're using. This usually happens in one of two ways:
    - A usdview bundle that includes its own copy (for example `usdviewTools\gizmo`), loaded
      because the whole `usdviewTools` folder is on the plugin path. List only the
      subfolders you want.
    - A saved **user-level** `PXR_PLUGINPATH_NAME` that points at an old plugins folder.
      Any launcher that adds to the variable picks it up. Check it with:

        ```powershell
        [Environment]::GetEnvironmentVariable("PXR_PLUGINPATH_NAME", "User")
        ```

## Running from a clone

If you want to run from a checkout of this repo without installing it (for example while
working on usd_chunks itself), add the repo root to `PYTHONPATH` so usdview's Python can
import the `usd_chunks` package:

```
set PYTHONPATH=C:\path\to\usd_chunks;%PYTHONPATH%
```

You still need `PXR_PLUGINPATH_NAME`. Without an install there's no `usdchunks` command, so
get the path with `python -m usd_chunks.cli plugin-path` instead.

## Environment variables

| Variable | Default | What it does |
| --- | --- | --- |
| `USD_CHUNK_DIRS` | the bundled chunks | The folders your chunk modules are in, separated by `;` on Windows and `:` on macOS/Linux. Each folder is searched, including subfolders, for `.py` files with a `main()` function. See [Writing a chunk](writing-a-chunk.md). If two folders have a chunk with the same name, the one in the **first** folder is used. A ready-made set of chunks ships with usd_chunks (the repository's `chunks/` folder) and is always searched after the folders you list, so yours win on a duplicate name. |
| `USD_CHUNK_DEPENDENCY_DIR` | `%LOCALAPPDATA%/usd_chunks/installed_deps` (`~/.cache/...` elsewhere) | Where the pip packages a chunk needs are installed, in a `py<version>` subfolder per Python (usdview and Blender run different ones). It's added to `sys.path` automatically. |
| `USD_CHUNK_AUTO_INSTALL` | unset (ask) | What happens when a chunk's `requirements` ([Writing a chunk](writing-a-chunk.md#config)) aren't installed. **Unset:** ask once per chunk ("OK to install requests for chunk AMDMaterialXLibrary?") -- usdview in a dialog, `usdchunks run` on an interactive terminal, Blender in a popup (with Allow Online Access on); a yes installs into `USD_CHUNK_DEPENDENCY_DIR` with progress shown, and a "no" stands until restart. CI and other non-interactive runs only warn. **`1`:** install without asking. **`0`:** never install, only warn. |
| `USD_CHUNK_LOAD_FILTER` | *(unset, so everything loads)* | Only load the chunk modules you list, for example `USD_CHUNK_LOAD_FILTER=transform,scatter_points`. A module loads if its name appears anywhere in the string, so `transform` also lets a module called `trans` load, but not one called `transform_ops`. Useful for a quick test with only a few chunks loaded. |
| `USD_CHUNK_VERBOSE` | off | When two chunk modules have the same name, the first `USD_CHUNK_DIRS` folder wins and the log only gets a one-line summary. Set this to log every clash, with the file that was used and the one that was skipped. |
| `PXR_PLUGINPATH_NAME` | — | Only needed for usdview. See [usdview integration](#usdview-integration). |
| `PYTHONPATH` | — | Only needed when running from a clone instead of an install. See [Running from a clone](#running-from-a-clone). |
| `USD_CHUNK_LOG_LEVEL` | `INFO` | usd_chunks' own log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. Any other value is treated as `INFO`. Set it to `DEBUG` to see what a reload or cook does, param by param. That includes why a [`config()`](writing-a-chunk.md#config) fell back to defaults, which is otherwise only a log line. The CLI, usdview, Blender and the graph frontends print these to the console; a script of your own calls `usd_chunks.configure_logging()` to do the same. |
| `USD_CHUNKS_COOK_CACHE` | on | For chunks whose module sets [`cache_cook`](writing-a-chunk.md#caching-a-slow-cook). `=0` never reads or writes the cache, `=refresh` cooks every time and rewrites what is stored. |
| `USD_CHUNKS_COOK_CACHE_DIR` | `%LOCALAPPDATA%/usd_chunks/cook_cache` (`~/.cache/usd_chunks/cook_cache` elsewhere) | Where cached cooks are kept. Safe to delete at any time. |
| `USD_CHUNKS_COOK_CACHE_MAX_GB` | `2` | The most the cook cache may use. Each time something is stored, results unread for 30 days are deleted, then the least recently read until the folder fits. |
| `USD_CHUNKS_PROBE_DELEGATES` | *(auto-detected)* | Whether to ask render delegates for their settings. By default this is skipped in a GUI host like usdview (when a `QApplication` is running), because loading a second render engine there can crash it. Scripts and tests do ask. `=1` always asks, `=0` never does. Note the name starts with `USD_CHUNKS_`, unlike the other variables. |

## Build documentation

The reference pages are generated from the source code, so usd_chunks has to be
importable when you build. Install the docs extra and build:

```
pip install -e ".[docs]"
set USD_CHUNK_DIRS=chunks
mkdocs serve
```

`mkdocs serve` runs the site locally, and `mkdocs build` writes it to `site/` instead. CI
doesn't build the docs, so if something is broken you'll find out when you publish (the
publish step below builds the site).

On Windows, `build_docs.bat` does both. With no arguments it runs the site locally. With
`--share` it builds the site and copies it to a folder you choose, such as a network share,
a mapped drive or a web server folder:

```
build_docs.bat --share \\server\share\usd_chunks_docs
build_docs.bat --share E:\www\usd_chunks --directory-urls
```

It adds and overwrites files but never deletes them. Add `--mirror` to also delete files
that are no longer part of the site. That only works if the destination is empty or was
published by this script before, and publishing into something that looks like a source
checkout is always refused.

By default it builds plain file names (`example.html`), which work both from a file share
and from a web server. A browser opening files from a share won't turn `/example/` into
`/example/index.html`, so the default mkdocs URLs would break there. If the site is served
by a web server such as nginx, you can pass `--directory-urls` for cleaner URLs.

Search only works when the site is served over http, because browsers block the search
index from loading over `file://`. Everything else works from a plain file share.

Run `docs/gen_ref_nav.py` to regenerate the `docs/reference` markdown files.
