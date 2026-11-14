# Houdini

In Houdini, every chunk is a LOP node in Solaris. You put chunks down from the Tab menu,
wire them to each other and to any other LOP, and their params are normal Houdini parms.

You need Houdini 21.0 and a clone of usd_chunks. Tested with Houdini 21.0 on Windows.

## Install

The commands below use `C:\src\usd_chunks` for the clone and Houdini 21.0.868. Change them
to match yours.

**Step 1. Clone usd_chunks:**

```
git clone https://github.com/HuginStudios/usd_chunks.git C:\src\usd_chunks
```

**Step 2. Install typing-extensions for Houdini:**

```
"C:\Program Files\Side Effects Software\Houdini 21.0.868\python311\python.exe" -m pip install --target "%LOCALAPPDATA%\usd_chunks\installed_deps\py311" typing-extensions
```

**Step 3. Point Houdini at the usd_chunks package:**

```
setx HOUDINI_PACKAGE_DIR C:\src\usd_chunks\usd_chunks\hosts\houdini\packages
```

If `HOUDINI_PACKAGE_DIR` is already set, for V-Ray say, don't use `setx`. Add the folder
to the end of it in Windows' **Environment Variables** window, after a `;`.

**Step 4. Start Houdini.** Open a LOP network, such as `/stage`, press Tab and type `mesh`.
**Mesh** shows up under **usdchunks**.

## Updating

`git pull`. Houdini reads usd_chunks straight from the clone, so there's nothing to
reinstall. The same setup works for every Houdini version that runs Python 3.11.

## Using chunks in Houdini

- **The Tab menu.** All chunks are under **usdchunks**, sorted into categories like
  Modelling and Transform.
- **Houdini owns the graph.** Chunks connect with normal LOP wires and cook when a parm
  or something upstream changes.
- **Params are Houdini parms.** They take keys and expressions and save with the `.hip`.
  To read another chunk's param, use `ch("../mesh1/radius")`.
- **Viewport handles.** Select a chunk node and press **Enter** in the viewport. Its box
  gizmos become Houdini's bounding box handle: drag a face and only that face moves, so a
  Blockout Box's faces snap to its Snap step. Its translate gizmos become move handles.
  Drags undo like any Houdini handle. Other gizmo types (rotate, scale, transform,
  scalar, brush) have no handle in Houdini yet, so use the parms for those.
- **Networks travel between apps.** Tab > usdchunks > **Chunk Import...** loads a network
  saved in usdview or Blender. A **Chunkgroup** node's **Export Network...** button writes
  one back.

The details, including what doesn't work in Houdini yet, are in the [Houdini host
README](https://github.com/HuginStudios/usd_chunks/blob/main/usd_chunks/hosts/houdini/README.md).

## If something is wrong

**The Tab menu has no chunks.** You only see **Chunk** and **Chunkgroup**, or nothing under
**usdchunks**. usd_chunks didn't load. The Houdini console says why, in a line starting
with `usd_chunks: not loaded`. The usual causes:

- **Step 3 didn't take.** `setx` only reaches programs started after it, so start Houdini
  again from a new window.
- **Step 2 is missing.** The console names `typing_extensions`.
- **`houdini.env` sets `PYTHONPATH`.** A `PYTHONPATH` line in
  `Documents\houdini21.0\houdini.env` replaces the package's value. Remove the line, or add
  the clone to it: `PYTHONPATH = C:/src/usd_chunks`.
- **Another USD is on `PYTHONPATH`.** A system-wide `PYTHONPATH` that points at a different
  USD build breaks Houdini's Python. Don't set `PYTHONPATH` system-wide.

**Step 2 fails with a pip error.** With no internet, or with a network that checks HTTPS
traffic (it fails with `CERTIFICATE_VERIFY_FAILED`), Houdini's pip can't reach PyPI. Run
the same command with any other Python 3 that can.

**A chunk is missing from the Tab menu.** The chunk needs a pip package that isn't
installed. When you put one down from the Tab menu, Houdini asks to install it. A `.hip`
that loads never asks; the node shows an error until the package is installed. See [If a
chunk is missing from the menu](install.md#if-a-chunk-is-missing-from-the-menu).

**usd_chunks loads twice.** If you set it up the old way, with `install.py`, delete
`Documents\houdini21.0\packages\usd_chunks.json`.

## Notes on the install

- **Why Houdini's `python311\python.exe`?** It's Houdini's own Python 3.11 without the
  Houdini parts, so it needs no licence. `hython` would also work, but needs a licence
  that can run it.
- **Why a separate folder for typing-extensions?** Houdini's own Python is shared by every
  plugin and replaced on upgrade. usd_chunks looks in `%LOCALAPPDATA%\usd_chunks\installed_deps\py311`
  by itself.
- **attrs is older than usd_chunks asks for.** Houdini 21 ships attrs 22.2 and usd_chunks
  asks for 25.4 or newer. usd_chunks works with Houdini's copy, so leave it alone.
- **Don't install `usd-core`** or the `[standalone]` extra for Houdini, and don't
  `pip install` usd_chunks into Houdini's Python. Houdini has its own USD.
- **Without the package.** For a launcher of your own, set these before starting Houdini
  instead of step 3: `HOUDINI_PATH` to `C:\src\usd_chunks\usd_chunks\hosts\houdini;&`
  (keep the `&`, and quote the `set` line in cmd) and `PYTHONPATH` to `C:\src\usd_chunks`.
- **`install.py`** is the old setup. It writes a package file into one Houdini version's
  `packages` folder instead.
