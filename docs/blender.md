# Blender

usd_chunks adds a **USDChunks Editor** node editor to Blender. You build a chunk network
there, and the 3D viewport shows the result.

You need Blender 4.2 or newer and a clone of usd_chunks. Tested with Blender 5.2 on
Windows.

## Install

Open cmd in the folder you cloned usd_chunks into:

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

To open a saved network, press N in the editor, go to the **USDChunks** tab and click
**Open Chunk Network**. **File > Import > USD** of a network file opens its network too, in
a node tree of its own.

To move things in the viewport, Ctrl+click a prim to pick it, then press G, R or S to move,
rotate or scale it, like a Blender object. X, Y or Z locks an axis. Click to confirm, Esc
to cancel. This changes the chunk's params. A prim no chunk drives gets a Transform chunk.
The handles are part of the add-on, so there is nothing else to install.

## Updating

After a `git pull`, run steps 1 to 3 again and restart Blender.

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
