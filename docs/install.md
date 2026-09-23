# Install

Two things make a usd_chunks setup work: the package importable in whatever Python runs
your host, and **USD_CHUNK_DIRS** pointed at your chunk modules. Everything else below is
detail on those two facts.

## Quick start

Given a zip or clone of this repo and a usdview build to point it at:

```
pip install usd-chunks                                   # into usdview's own Python
usdchunks plugin-path                                     # copy what this prints
set PXR_PLUGINPATH_NAME=<what it printed>                 # Windows (cmd)
set USD_CHUNK_DIRS=C:\path\to\usd_chunks\scripts          # Windows (cmd)
usdview anything.usda
```

(`$env:NAME = "value"` on PowerShell, `export NAME=value` on macOS/Linux.) Opening usdview
with those two variables set gets you a **Chunks** panel and menu; right-click the graph
canvas and every chunk under [`scripts/`](/scripts/) — this repo's own chunk library — is
there to drop in. Point `USD_CHUNK_DIRS` at your own directory once you're writing chunks
of your own; multiple directories are `;`-separated on Windows and `:`-separated
elsewhere, and nested folders are scanned too.

No usdview build handy, or want it headless (CI, a batch cook)? Skip `PXR_PLUGINPATH_NAME`
and usdview entirely:

```
pip install "usd-chunks[standalone]"
set USD_CHUNK_DIRS=C:\path\to\usd_chunks\scripts
usdchunks run --help
```

`usdchunks run` needs a `.usda` chunk network to act on — save one from usdview first, or
see the `run` examples in the [README](/README.md#the-same-definition-runs-interactively-and-headless).
`usdchunks run --help` only proves the install itself worked, not that your chunks were
found — that only shows up once you point `run` (or usdview) at a real network.

## Install

The one question that actually matters: **does the Python you're installing into already
have USD?** Run `python -c "import pxr"` — if that already works (you're inside usdview,
Houdini's `hython`, Maya's `mayapy`, Blender's bundled Python, or any other DCC), you
already have a USD, and installing another one is the wrong move:

```
pip install usd-chunks
```

If it fails (a plain script, a CI job, a bare venv with no DCC in sight), you need to bring
your own USD:

```
pip install "usd-chunks[standalone]"
```

The `standalone` extra pulls in `usd-core`, giving you the library plus the `usdchunks`
command with no separate USD build required:

```
usdchunks run my_setup.usda
```

**Never install the `standalone` extra into a host that already has USD.** Houdini's
`hython`, Maya's `mayapy`, Blender's Python and usdview all embed USD under their own
build — Blender's is compiled as `pxrBlender_v26_03__pxrReserved__`, for instance — so a
second, pip-installed USD is a different C++ type entirely and cannot exchange objects
with the host's. You end up with two unrelated USDs sharing one process rather than one
shared USD, and things fail in confusing, not-obviously-related ways.

Note that the `usd-core` wheels on PyPI do not include usdview — it is not part of that
build. Using the usdview integration needs a full USD build that provides
`pxr.Usdviewq`; see below.

### Other extras

The rest only matter for specific work, and combine (`pip install -e ".[dev,docs]"`):

| Extra | Who's it for |
| --- | --- |
| `ui` | A host that doesn't already ship Qt — pulls in PySide6. usdview and Houdini already have Qt, skip this there. |
| `flet` | Running the standalone Flet graph frontend (`usdchunks-flet`) rather than embedding in a Qt host. |
| `imgui` | Same, for the Dear ImGui frontend (`usdchunks-imgui`). |
| `dev` | Working on usd_chunks itself — pytest, ruff, build tooling, plus `usd-core` and every optional frontend toolkit so the test suite can exercise all of them. |
| `docs` | Building the docs site — see [Build documentation](#build-documentation). |

## usdview integration

usdview loads usd_chunks as a USD plugin, so it needs to be told where that plugin's
`plugInfo.json` lives, via `PXR_PLUGINPATH_NAME`. `usdchunks` prints the right value for
your install:

```
usdchunks plugin-path
```

Point `PXR_PLUGINPATH_NAME` at that path before launching usdview. With that set and
`USD_CHUNK_DIRS` pointed at your chunks, opening usdview shows a **Chunks** panel and a
**Chunks** menu.

## Gizmos (viewport handles)

A chunk can declare a viewport handle — a transform gizmo you drag to change a param
directly in the 3D view — via `gizmos` in its `config()` (see [Writing a
chunk](writing-a-chunk.md#config)). That's a two-repo split, one direction of dependency:

- **usd_chunks** (this repo) only **authors** the handle: `ui:gizmo:*` schema attributes
  saying what handle exists, which prim it hangs off, and which params it drives. Nothing
  here draws, picks or drags anything — a chunk with `gizmos` set works identically with or
  without a viewport implementation present, just with no visible handle in the latter case.
- **[usd_gizmos](https://github.com/HuginStudios/usd_gizmos)** is the host-side piece that
  draws, picks and drags the handle in usdview's viewport. It's a separate clone, not a pip
  package: check it out alongside usd_chunks, then add **both** to your environment —

```
set PYTHONPATH=C:\path\to\usd_gizmos;%PYTHONPATH%
set PXR_PLUGINPATH_NAME=<usd_chunks plugin path>;<usd_gizmos plugin path>
```

  `PXR_PLUGINPATH_NAME` takes a list the same way `USD_CHUNK_DIRS` does (`;` on Windows,
  `:` elsewhere) — usd_chunks's own entry still comes from `usdchunks plugin-path`,
  usd_gizmos's from wherever its `plugInfo.json` lives in your clone. usd_chunks is an
  *optional* import in usd_gizmos, not a hard dependency — it runs fine without this repo
  installed at all, it just then drives no chunk params.

  If your usdview bundle also ships an older, flattened gizmo plugin (bundled loosely under
  something like `usdviewTools/gizmo` rather than as its own package), don't load both —
  they fight over the same viewport overrides. Load `usd_gizmos` and leave the flattened
  copy out of `PXR_PLUGINPATH_NAME`.

See [Chunk gizmos: the host contract](gizmo-host-contract.md) for the full interface, if
you're building this piece for a different host or DCC.

## Running from a clone

If you're running against a checkout of this repo rather than an install (working on
usd_chunks itself, or on a fork), add the repo root to `PYTHONPATH` instead of installing
it, so usdview's Python can import the `usd_chunks` package:

```
set PYTHONPATH=C:\path\to\usd_chunks;%PYTHONPATH%
```

`PXR_PLUGINPATH_NAME` still applies the same way — `usdchunks plugin-path` works from a
clone too.

## Environment variables

| Variable | Default | What it does |
| --- | --- | --- |
| `USD_CHUNK_DIRS` | *(empty)* | Where chunk modules live. A list of directories — `;`-separated on Windows, `:`-separated on unix. Scanned recursively for `.py` files with a `main()` function; see [Writing a chunk](writing-a-chunk.md). On a duplicate chunk name the **first** directory in the list wins. |
| `GLOBAL_USDCHUNKS` | `~/.usdchunks/global` | Where a chunk store (`usdchunks install`) deposits chunks. Scanned the same way as `USD_CHUNK_DIRS`, but appended **after** it — your own directories always win a name clash against anything installed here, and it's skipped entirely if it doesn't exist yet (a clean machine gets no "directory not found" noise). |
| `USD_CHUNK_DEPENDENCY_DIR` | `<usd_chunks package>/../_installed_deps` | Where `USD_CHUNK_AUTO_INSTALL` installs a chunk's declared pip requirements. Added to `sys.path` automatically if it exists, so packages installed there are importable without any other setup. |
| `USD_CHUNK_AUTO_INSTALL` | off | Off unless set. A chunk module may declare `requirements` in its `config()` ([Writing a chunk](writing-a-chunk.md#config)); with this on, missing ones are installed (after an interactive confirmation) into `USD_CHUNK_DEPENDENCY_DIR`. Left off, nothing is installed and a missing package is only named in a warning — so opening someone else's network can never add packages to your environment on its own. Also gates headless `usdchunks run`, which has no one to answer a confirmation prompt. |
| `USD_CHUNK_LOAD_FILTER` | *(unset — everything loads)* | Restricts which chunk modules load, by substring match against the module's name — e.g. `USD_CHUNK_LOAD_FILTER=transform,scatter` loads any module whose name contains `"transform"` or `"scatter"`. Useful for a fast, minimal repro without every other chunk in `USD_CHUNK_DIRS` also registering. |
| `USD_CHUNK_VERBOSE` | off | Off unless set (truthy). A **name clash** between two chunk modules resolves silently by precedence (first `USD_CHUNK_DIRS` entry, then `GLOBAL_USDCHUNKS` — see above); with this on, every shadowed name is logged at load, not just the ambiguous same-tier ones. |
| `PXR_PLUGINPATH_NAME` | — | Only needed for the usdview integration. See [usdview integration](#usdview-integration). |
| `PYTHONPATH` | — | Only needed when running from a clone rather than an install. See [Running from a clone](#running-from-a-clone). |

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
