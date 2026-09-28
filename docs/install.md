# Install

To get usd_chunks working you need two things: the package installed in the Python your
host runs, and **USD_CHUNK_DIRS** set to the folder with your chunk modules. The rest of
this page is detail on those two steps.

## Quick start

You need **Python 3.11+**, and it has to be the same Python your USD was built with (see
[Requirements](#requirements)). On Windows (cmd), where `python` is usdview's own Python:

```
git clone https://github.com/HuginStudios/usd_chunks.git
python -m pip install -e ./usd_chunks
for /f "delims=" %i in ('usdchunks plugin-path') do set PXR_PLUGINPATH_NAME=%i
set USD_CHUNK_DIRS=C:\path\to\usd_chunks\chunks
usdview anything.usda
```

If `pip install -e` fails, your pip is probably too old for editable installs. Run
`python -m pip install --upgrade pip` and try again.

To run without usdview, using `usd-core` only:

```
git clone https://github.com/HuginStudios/usd_chunks.git
python -m pip install -e "./usd_chunks[standalone]"
set USD_CHUNK_DIRS=C:\path\to\usd_chunks\chunks
usdchunks run --help
```

In PowerShell, use `$env:PXR_PLUGINPATH_NAME = (usdchunks plugin-path)` and
`$env:NAME = "value"` for the other variables. On macOS/Linux, use
`export PXR_PLUGINPATH_NAME="$(usdchunks plugin-path)"` and `export NAME=value`.

In usdview, right-click the graph canvas (or press Tab) to see every chunk in
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
  Python: usdview's, Houdini's `hython`, Maya's `mayapy`, and so on. If your USD was built
  with a Python older than 3.11, you need to rebuild USD with 3.11 or newer first.

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

**If that works**, you're in usdview, Houdini's `hython`, Maya's `mayapy`, Blender's
Python or another DCC. Install it as is:

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
need a full USD build that includes `pxr.Usdviewq`. See [usdview
integration](#usdview-integration) below.

### ⚠️ A plain install won't show every bundled chunk

!!! warning
    A plain install doesn't include numpy, PySide6 or requests, and about a sixth of the
    chunks in [`chunks/`](https://github.com/HuginStudios/usd_chunks/tree/main/chunks)
    need one of them.

If a chunk module fails to import, it's left out of the create menu. The registry logs
what's missing as a `WARNING` at the default log level, but nothing else tells you. Usually
the first thing you notice is that the chunk you want isn't in the menu.

The chunks affected today: `AMDMaterialXLibrary`, `add_curves`, `add_mesh`,
`curves_to_mesh`, `custom_widget`, `poly_reduce`, `rbd_solver`, `scatter_brush`,
`scatter_points`, `urdf_import`, `uv_checker` and `vine`. That includes some of the main
ones, like Scatter, Poly Reduce and the RBD solver.

There are two ways to fix it:

- Install the packages yourself: `pip install numpy requests mujoco` (mujoco is only for
  the RBD solver). Add `PySide6` only if your host doesn't have Qt already; usdview,
  Houdini and Maya do. You can also install just the ones you need.
- Set [`USD_CHUNK_AUTO_INSTALL`](#environment-variables). Then a chunk's own
  `requirements` ([Writing a chunk](writing-a-chunk.md#config)) are installed for you,
  after asking first.

### Other extras

These are only needed for specific work. You can combine them, for example
`pip install -e ".[dev,docs]"`.

| Extra | What it's for |
| --- | --- |
| `ui` | Installs PySide6, for a host that doesn't have Qt. usdview and Houdini already have Qt, so skip it there. |
| `flet` | Running the standalone Flet graph frontend (`python -m usd_chunks.frontends.flet.nodegraph <network.usda>`) instead of a Qt host. |
| `imgui` | The same for the Dear ImGui frontend (`python -m usd_chunks.frontends.imgui.nodegraph <network.usda>`). |
| `dev` | Working on usd_chunks itself: pytest, ruff, build tools, `usd-core` and every optional frontend, so the tests can cover all of them. |
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
hython -m pip install -e usd_chunks            # Houdini
mayapy -m pip install -e usd_chunks            # Maya
```

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

The panel is there, but a chunk like Scatter, Add Mesh or the RBD solver isn't in the
create menu. This is almost always a missing pip package: the chunk failed to import, so
it was left out. The console shows which package when it loads:

```
WARNING: usd_chunks.chunk_registry: Chunk dependencies not installed: numpy>=2,<3. ...
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

You still need `PXR_PLUGINPATH_NAME`. `usdchunks plugin-path` works from a clone too.

## Environment variables

| Variable | Default | What it does |
| --- | --- | --- |
| `USD_CHUNK_DIRS` | *(empty)* | The folders your chunk modules are in, separated by `;` on Windows and `:` on macOS/Linux. Each folder is searched, including subfolders, for `.py` files with a `main()` function. See [Writing a chunk](writing-a-chunk.md). If two folders have a chunk with the same name, the one in the **first** folder is used. |
| `USD_CHUNK_DEPENDENCY_DIR` | `<usd_chunks package>/../_installed_deps` | Where `USD_CHUNK_AUTO_INSTALL` installs the pip packages a chunk needs. If the folder exists, it's added to `sys.path` automatically. |
| `USD_CHUNK_AUTO_INSTALL` | off | A chunk module can list `requirements` in its `config()` ([Writing a chunk](writing-a-chunk.md#config)). With this set, missing packages are installed into `USD_CHUNK_DEPENDENCY_DIR` after asking you first. When it's off, nothing is installed and a missing package only shows up as a warning, so opening someone else's network can never install packages on its own. It also applies to `usdchunks run`, which can't ask. |
| `USD_CHUNK_LOAD_FILTER` | *(unset, so everything loads)* | Only load the chunk modules you list, as comma-separated exact module names, for example `USD_CHUNK_LOAD_FILTER=transform,scatter_points`. The check is `module_name not in USD_CHUNK_LOAD_FILTER`, so a module loads if its name appears somewhere in the string. That means `transform_ops` would **not** load with `transform` alone. Useful for a quick test with only a few chunks loaded. |
| `USD_CHUNK_VERBOSE` | off | When two chunk modules have the same name, one is picked without a message (the first `USD_CHUNK_DIRS` folder wins, see above). Set this to log every chunk that was skipped because of a name clash. |
| `PXR_PLUGINPATH_NAME` | — | Only needed for usdview. See [usdview integration](#usdview-integration). |
| `PYTHONPATH` | — | Only needed when running from a clone instead of an install. See [Running from a clone](#running-from-a-clone). |
| `USD_CHUNK_LOG_LEVEL` | `INFO` | usd_chunks' own log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. Any other value is treated as `INFO`. Set it to `DEBUG` to see what a reload or cook does, param by param. That includes why a [`config()`](writing-a-chunk.md#config) fell back to defaults, which is otherwise only a log line. |
| `USD_CHUNKS_PROBE_DELEGATES` | *(auto-detected)* | Whether to ask render delegates for their settings. By default this only happens in a GUI host (when a `QApplication` is running), not in scripts or tests. `=1` turns it on, `=0` turns it off. Note the name starts with `USD_CHUNKS_`, unlike the other variables. |

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
