# Blender

usd_chunks adds a **USDChunks Editor** node editor to Blender. You build a chunk network
there, and the 3D viewport shows the result.

You need Blender 4.2 or newer. Tested with Blender 5.1 and 5.2 on Windows.

## Install

1. Download `usd_chunks_blender-<version>.zip` from the
   [Releases page](https://github.com/HuginStudios/usd_chunks/releases). Don't unzip it.
2. In Blender, go to **Edit > Preferences > Add-ons**, open the **⌄** menu at the top right,
   choose **Install from Disk...** and pick the zip.

That is all: usd_chunks and its chunks are inside the zip, and Blender has its own USD.
To use your own chunks as well, set `USD_CHUNK_DIRS` to their folders before starting
Blender.

**Try it:** switch any area to **USDChunks Editor**, click **New**, and press Shift+A to add
a chunk.

## Install from a clone

To install a version that isn't released yet. Open cmd in the folder you cloned usd_chunks
into:

```
cd C:\src\usd_chunks
```

The commands below are for Blender 5.2. For another version, change `5.2` in the paths.

**Step 1. Install usd_chunks into Blender:**

```
"C:\Program Files\Blender Foundation\Blender 5.2\5.2\python\bin\python.exe" -m pip install --no-deps --upgrade --target "%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\modules" .
```

**Step 2. Build the add-on:**

```
mkdir dist 2>nul
"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --command extension build --source-dir usd_chunks\frontends\blender\addon --output-dir dist
```

This makes `dist\usd_chunks-0.1.0.zip`.

**Step 3. Install the add-on:** in Blender, go to **Edit > Preferences > Add-ons**, open
the **⌄** menu at the top right, choose **Install from Disk...** and pick
`dist\usd_chunks-0.1.0.zip`.

**Step 4. Try it:** switch any area to **USDChunks Editor**, click **New**, and press
Shift+A to add a chunk.

To join a branch into a chain, drag a node with no wires onto a wire: it goes in through a
Merge, the dropped node winning, as in the other graph editors.

To open a saved network, press N in the editor, go to the **USDChunks** tab and click
**Open Chunk Network**. **File > Import > USD** of a network file opens its network too, in
a node tree of its own.

To move things in the viewport, Ctrl+click a prim to pick it, then press G, R or S to move,
rotate or scale it, like a Blender object. X, Y or Z locks an axis, named as in Blender:
Z is up. Click to confirm, Esc to cancel. This changes the chunk's params. A prim no chunk drives gets a Transform chunk.
The handles are part of the add-on, so there is nothing else to install.

A chunk with a box handle (Blockout Box, Blocking Volume) shows a wire box when you pick
its prim or select its node in the editor. Hover a face to light it, then drag it to move
that face. Esc puts it back.

A network saved Y-up (usdview's default) is turned to stand up in Blender's Z-up viewport,
as Blender's USD importer does, and scaled by its `metersPerUnit`. The file itself is not
changed: chunks still read and write in the network's own space.

## Updating

Install the newer zip the same way and restart Blender; it replaces the old one. From a
clone: after a `git pull`, run steps 1 to 3 again and restart Blender.

## Working from a checkout

For people working on usd_chunks itself. This links Blender to your clone, so a
`git pull` or an edit shows up after a restart, with no reinstall.

1. Remove the installed copies: uninstall **USDChunks Editor** in Preferences, and delete
   the `usd_chunks` folder from `%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\modules`.
2. In cmd, run:

    ```
    mklink /J "%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\modules\usd_chunks" C:\src\usd_chunks\usd_chunks
    mklink /J "%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\usd_chunks" C:\src\usd_chunks\usd_chunks\frontends\blender\addon
    setx USD_CHUNK_DIRS C:\src\usd_chunks\chunks
    ```

3. Start Blender and turn on **USDChunks Editor** in Preferences.

## Other renderers in the live view

The live view draws with Storm, the renderer Blender ships. Another Hydra render delegate
can draw it instead, if it was built against **Blender's own USD**: a build for any other
USD won't load inside Blender.

1. In **Edit > Preferences > Add-ons > USDChunks Editor**, set **Hydra Delegate Folders**
   to the delegate's folder: the one with its `plugInfo.json`, or with a `resources`
   folder that has it. Separate several folders with `;`.
2. In the 3D viewport, open the sidebar (N) and pick it in **USDChunks > Renderer**.

A launcher can set `USD_CHUNKS_HYDRA_DELEGATES` to the same list instead. The add-on reads
it when it starts.

The live view uses the stage's RenderSettings prim: the one the stage's
`renderSettingsPrimPath` names, or else the first one under `/Render`. The renderer's own
settings on it, such as Aurora's `aurora:max_samples` or `aurora:is_denoising_enabled`,
apply to the live view. A path tracer keeps refining the image until it is done.

## If something is wrong

**"No module named 'usd_chunks'" when the add-on turns on.** Step 1 didn't run, or used
another Blender version's folder. Run step 1, then turn the add-on off and on again.

**A node is red and says "Missing chunk type".** The network uses a chunk that no folder
on `USD_CHUNK_DIRS` has, such as one from your own chunk library. Its node keeps its wires
and the values saved in the file. Add the folder to `USD_CHUNK_DIRS` and restart Blender.
The USDChunks tab in the sidebar lists every missing type.

**A chunk is missing from Shift+A.** It needs a pip package that isn't installed. Use the
chunk and Blender asks to install it. That needs **Allow Online Access** on, in **Edit >
Preferences > System > Network**.

## Notes on the install

- **Why not a plain `pip install`?** Blender is in `Program Files`, which you can't write
  to. pip then puts the package in a per-user folder that Blender doesn't read. Step 1
  installs into Blender's own add-on modules folder instead.
- **`--no-deps`** keeps the attrs and typing-extensions Blender ships. Blender 5.2's attrs
  is 25.3, older than the 25.4 usd_chunks asks for, but it works.
- **`--upgrade`** replaces the copy from an earlier install.
- **Step 1 comes before step 3.** Blender turns the add-on on as soon as it's installed,
  and that fails if usd_chunks isn't there yet.
- **The `mklink` links** need no admin rights. A linked usd_chunks can't find its bundled
  chunks by itself, so `USD_CHUNK_DIRS` points at them. `setx` only reaches programs
  started after it.
- **Never install `usd-core`** or the `[standalone]` extra for Blender, and don't start
  Blender with `--python-use-system-env`. Blender has its own USD, and a second one in the
  same Blender breaks in confusing ways.
- **Other systems.** To print Blender's Python and the folder for step 1:

    ```
    blender -b --python-expr "import bpy, sys; print(sys.executable); print(bpy.utils.user_resource('SCRIPTS', path='addons/modules', create=True))"
    ```
