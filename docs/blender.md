# Blender

In Blender, usd_chunks adds a **USDChunks Editor** node editor. A node tree there is a
chunk network saved as a `.usda` file. The 3D viewport draws that network's USD stage live.

Tested with Blender 5.2 on Windows.

## What you need

- **Blender 4.2 or newer.**
- **A clone of usd_chunks.** The examples below use `C:\src\usd_chunks`.

Nothing else. Blender has its own USD, attrs and typing-extensions. Never install
`usd-core` or the `[standalone]` extra for Blender: Blender's USD is built under its own
name, and a second USD can't share objects with it.

## Install

There are two parts: the usd_chunks package, then the add-on. Install the package first.
Blender turns the add-on on as soon as you install it, and that fails if the package isn't
there yet.

### 1. Install the package

A plain `pip install` into Blender's Python doesn't work on a normal Windows install.
Blender is in `Program Files`, which you can't write to, so pip puts the package in a
per-user folder that Blender never reads. Install it into Blender's own add-on modules
folder instead.

To print the two paths you need, Blender's Python and that folder:

```
"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" -b --python-expr "import bpy, sys; print(sys.executable); print(bpy.utils.user_resource('SCRIPTS', path='addons/modules', create=True))"
```

For Blender 5.2 on Windows they are:

- `C:\Program Files\Blender Foundation\Blender 5.2\5.2\python\bin\python.exe`
- `%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\modules`

Then, in cmd, from the clone's folder:

```
cd C:\src\usd_chunks
"C:\Program Files\Blender Foundation\Blender 5.2\5.2\python\bin\python.exe" -m pip install --no-deps --upgrade --target "%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\modules" .
```

- `--no-deps` keeps Blender's own attrs and typing-extensions. Blender 5.2 ships attrs
  25.3, older than the 25.4 usd_chunks asks for, but it works.
- `--upgrade` replaces an older copy, so the same command works for updates.

This copies usd_chunks and its bundled chunks into Blender's folder.

### 2. Install the add-on

Build the add-on zip with Blender's own tool, from the clone's folder:

```
"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --command extension build --source-dir usd_chunks\frontends\blender\addon --output-dir dist
```

This makes `dist\usd_chunks-0.1.0.zip`. Then in Blender: **Edit > Preferences > Add-ons**,
open the **⌄** menu at the top right, choose **Install from Disk...** and pick the zip.
**USDChunks Editor** shows up in the add-on list, turned on.

You can also install it from the command line:

```
"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --command extension install-file -r user_default -e dist\usd_chunks-0.1.0.zip
```

## Check that it works

1. In any area, open the editor type menu and choose **USDChunks Editor**.
2. Press N to open the sidebar and go to the **USDChunks** tab. Click **Open Chunk
   Network** and pick a network saved in usdview, Houdini or by a script (see
   [Scripting](scripting.md)). Its nodes show up in the editor, and the 3D viewport draws
   the result. **New Chunk Network** starts an empty one.
3. Press Shift+A to add chunks. They're sorted by category, like Modelling and Transform.

The 3D viewport's sidebar also has a **USDChunks** tab. It turns the stage drawing on and
off and sets shading and lighting. Ctrl+click in the viewport picks a USD prim.

## Updating

After a `git pull`, run both install steps again and restart Blender. Step 1 and step 2
each make a copy, so Blender doesn't see the new code until you do.

## Working from a checkout

If you work on usd_chunks itself, link Blender to your clone instead of copying, so a
`git pull` or an edit reaches Blender after a restart.

First remove the copies: uninstall **USDChunks Editor** in Preferences, and delete
`usd_chunks` from the `addons\modules` folder. Then, in cmd (no admin needed):

```
mklink /J "%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\modules\usd_chunks" C:\src\usd_chunks\usd_chunks
mklink /J "%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\usd_chunks" C:\src\usd_chunks\usd_chunks\frontends\blender\addon
setx USD_CHUNK_DIRS C:\src\usd_chunks\chunks
```

The first link is the package and the second the add-on. A linked package can't find the
bundled chunks by itself, so `USD_CHUNK_DIRS` points at them. `setx` saves it for new
programs, so start Blender again from a new window. Then turn on **USDChunks Editor** in
Preferences.

## If something is wrong

### "No module named 'usd_chunks'" when the add-on turns on

Step 1 is missing, or went into the wrong folder. The folder has Blender's version in it
(`...\Blender\5.2\...`), so it must match the Blender you run. Print the right one with the
command in step 1. If you installed the add-on first, install the package, then turn the
add-on off and on again.

### Blender started with `--python-use-system-env`

Don't use it with usd_chunks. It makes Blender read `PYTHONPATH`, and if that has another
USD on it, you get two USDs in one Blender.

### A chunk is missing from Shift+A

The chunk needs a pip package that isn't installed. When you use it, Blender asks in a
popup whether to install it. That needs **Allow Online Access** on, in **Edit >
Preferences > System > Network**. See [If a chunk is missing from the
menu](install.md#if-a-chunk-is-missing-from-the-menu).
