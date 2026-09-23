# Install

Two things make a usd_chunks setup work: the package importable in whatever Python runs
your host, and **USD_CHUNK_DIRS** pointed at your chunk modules. Everything else below is
detail on those two facts.

## Quickest path

Given a zip or clone of this repo and nothing else:

```
pip install "usd-chunks[standalone]"
set USD_CHUNK_DIRS=C:\path\to\usd_chunks\scripts        # Windows (cmd)
$env:USD_CHUNK_DIRS = "C:\path\to\usd_chunks\scripts"    # Windows (PowerShell)
export USD_CHUNK_DIRS=/path/to/usd_chunks/scripts        # macOS / Linux
usdchunks run --help
```

`scripts/` is this repo's own chunk library — set `USD_CHUNK_DIRS` to it to get every
chunk under [`scripts/`](/scripts/) available immediately, or to your own directory once
you're writing chunks of your own. Multiple directories are `;`-separated on Windows and
`:`-separated elsewhere; nested folders are scanned too.

That's enough to use `usdchunks run` headlessly. To use the graph and panels inside
usdview, see [usdview integration](#usdview-integration) below.

## Install

```
pip install "usd-chunks[standalone]"
```

The `standalone` extra brings OpenUSD itself (`usd-core`), giving you the library plus the
`usdchunks` command with no USD build required:

```
usdchunks run my_setup.usda
```

Ask for the extra only when running **outside** a DCC. Inside a host that already ships
USD — usdview, Houdini, Maya, Blender — plain `pip install usd-chunks` into that host's
interpreter is what you want: it uses the USD already there. Installing `usd-core`
alongside a host's USD does not share anything with it. Hosts build USD under a private
namespace (Blender's is `pxrBlender_v26_03__pxrReserved__`), so the two are different C++
types and cannot exchange objects — you end up with two unrelated USDs in one process.

Note that the `usd-core` wheels on PyPI do not include usdview — it is not part of that
build. Using the usdview integration needs a full USD build that provides
`pxr.Usdviewq`; see below.

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
