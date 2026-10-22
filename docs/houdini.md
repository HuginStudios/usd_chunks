# Houdini

In Houdini, every chunk is a LOP node in Solaris. You put chunks down from the Tab menu,
wire them to each other and to any other LOP, and their params are normal Houdini parms.

Tested with Houdini 21.0 on Windows.

## What you need

- **Houdini 21.0.** It runs Python 3.11, which usd_chunks needs.
- **A clone of usd_chunks.** The examples below use `C:\src\usd_chunks`.

Nothing else. Houdini has its own USD and Qt. Don't install `usd-core` or the
`[standalone]` extra for Houdini, and don't `pip install` usd_chunks into Houdini's Python.

## Install

1. Clone the repo:

    ```
    git clone https://github.com/HuginStudios/usd_chunks.git C:\src\usd_chunks
    ```

2. Run the installer with Houdini's own `hython`, from the clone's folder. In cmd:

    ```
    cd C:\src\usd_chunks
    "C:\Program Files\Side Effects Software\Houdini 21.0.868\bin\hython.exe" usd_chunks\hosts\houdini\install.py
    ```

    In PowerShell, put `&` before the quoted `hython.exe` path. Use your own Houdini
    version in the path.

3. Restart Houdini.

The installer does two things:

- It installs `typing-extensions`, which Houdini doesn't ship, into the usd_chunks
  dependency folder (`%LOCALAPPDATA%\usd_chunks\installed_deps\py311`). Houdini's own
  Python is left alone.
- It writes a package file, `usd_chunks.json`, into your Houdini `packages` folder
  (`Documents\houdini21.0\packages`). This file tells Houdini where usd_chunks and its
  chunks are. The installer prints the full path when it's done.

The package points at your clone, so a `git pull` is all you need to update. Run the
installer again only if you move the clone or want different chunk folders.

Installer options:

| Option | What it does |
| --- | --- |
| `--chunk-dirs DIR ...` | The chunk folders to use. Default: the clone's `chunks\` folder. |
| `--no-deps` | Don't install `typing-extensions`. |
| `--deps-only` | Only install `typing-extensions`, don't write the package file. For a launcher that sets the environment itself (see below). |

## Check that it works

1. Open a LOP network, such as `/stage`.
2. Press Tab and type `mesh`. **Mesh** shows up under **usdchunks**. All chunks are in
   the Tab menu under **usdchunks**, sorted into categories like Modelling and Transform.
3. Put down a Mesh, then a Transform below it. Set the Transform's **Prim Path** to the
   mesh (`/mesh`) and change **Translate**. The mesh moves in the viewport.

## Using chunks in Houdini

- **Houdini owns the graph.** Chunks connect with normal LOP wires and cook when a parm
  or something upstream changes.
- **Params are Houdini parms.** They take keys and expressions and save with the `.hip`.
  To read another chunk's param, use `ch("../mesh1/radius")`.
- **Networks travel between apps.** Tab > usdchunks > **Import Network...** loads a network
  saved in usdview or Blender. A **Chunkgroup** node's **Export Network...** button writes
  one back.

The details, including what doesn't work in Houdini yet, are in the [Houdini host
README](https://github.com/HuginStudios/usd_chunks/blob/main/usd_chunks/hosts/houdini/README.md).

## Without the package file

To try usd_chunks for one session, or from your own launcher, set these before you start
Houdini instead of running the full installer:

| Variable | Set to |
| --- | --- |
| `HOUDINI_PATH` | `C:\src\usd_chunks\usd_chunks\hosts\houdini;&` (keep the `&`) |
| `PYTHONPATH` | `C:\src\usd_chunks`, added at the front |
| `USD_CHUNK_DIRS` | `C:\src\usd_chunks\chunks`, added at the front |

In cmd:

```bat
set HFS=C:\Program Files\Side Effects Software\Houdini 21.0.868
set "HOUDINI_PATH=C:\src\usd_chunks\usd_chunks\hosts\houdini;&"
set PYTHONPATH=C:\src\usd_chunks;%PYTHONPATH%
set USD_CHUNK_DIRS=C:\src\usd_chunks\chunks;%USD_CHUNK_DIRS%
"%HFS%\bin\hython.exe" C:\src\usd_chunks\usd_chunks\hosts\houdini\install.py --deps-only
"%HFS%\bin\houdini.exe"
```

Quote the `HOUDINI_PATH` line, or cmd reads the `&` as a new command. If `HOUDINI_PATH` is
already set, add to it instead:
`set "HOUDINI_PATH=C:\src\usd_chunks\usd_chunks\hosts\houdini;%HOUDINI_PATH%"`. The
`--deps-only` run is only needed once.

## If something is wrong

### The Tab menu has no chunks

You only see **Chunk** and **Chunkgroup**, or nothing under **usdchunks**. usd_chunks
didn't import. The Houdini console says why, in a line starting with
`usd_chunks: not loaded`. The usual causes:

- **`houdini.env` sets `PYTHONPATH`.** A `PYTHONPATH` line in
  `Documents\houdini21.0\houdini.env` replaces the package's value. Remove the line, or add
  the clone to it: `PYTHONPATH = C:/src/usd_chunks`.
- **Another USD is on `PYTHONPATH`.** A system-wide `PYTHONPATH` that points at a different
  USD build breaks Houdini's Python. Don't set `PYTHONPATH` system-wide.
- **The installer wasn't run, or ran for another Houdini version.** The package file goes
  into one version's folder, such as `houdini21.0`. Run the installer with that version's
  `hython`.

### The package file went to the wrong folder

Git Bash sets `HOME`, and Houdini then uses `C:\Users\<you>\houdini21.0` instead of
`Documents\houdini21.0`. Run the installer from cmd or PowerShell. It prints where it wrote
the file, so you can check.

### The installer stops with a pip error

It needs to download `typing-extensions` from PyPI. With no internet, or with a network
that checks HTTPS traffic (it fails with `CERTIFICATE_VERIFY_FAILED`), Houdini's pip can't
reach it. Install it with any other Python 3 that can, into the same folder, then run the
installer again:

```
python -m pip install --target "%LOCALAPPDATA%\usd_chunks\installed_deps\py311" typing-extensions
```

### A chunk is missing from the Tab menu

The chunk needs a pip package that isn't installed. When you put one down from the Tab
menu, Houdini asks to install it. A `.hip` that loads never asks; the node shows an error
until the package is installed. See [If a chunk is missing from the
menu](install.md#if-a-chunk-is-missing-from-the-menu).

### attrs is older than usd_chunks asks for

Houdini 21 ships attrs 22.2 and usd_chunks asks for 25.4 or newer. usd_chunks works with
Houdini's copy, so the installer leaves it alone. Don't replace it.
