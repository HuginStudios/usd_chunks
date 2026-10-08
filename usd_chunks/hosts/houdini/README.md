# Houdini (Solaris)

Every chunk is a LOP. In a LOP network press TAB and look under **usdchunks**, where chunks
sit in their categories (Modelling, Transforms, USD, ...), or just type a chunk's name.

- **Houdini owns the graph.** Chunk nodes wire to each other and to any Solaris node with
  ordinary LOP connections; a chunk reads its input stage and adds its own layer on top.
  usd_chunks' own wiring (exec inputs, `@Node.param@`) is not used here -- reference another
  node's parm with Houdini's `ch("../mesh1/radius")` instead.
- **Params are native parms.** Sliders, toggles, menus, colour and vector parms, folders,
  multiparms for lists, file choosers, and Solaris's prim picker for prim path params. They
  key, take expressions and save with the .hip like any other parm.
- **Houdini decides when a chunk cooks**: a parm change or an upstream change recooks it.

Tested with Houdini 21.0.

## Setup

With the usdbundle, start Houdini with `usdbundle\launchhoudini.bat`. It sets
`HOUDINI_PATH`, `PYTHONPATH` and `USD_CHUNK_DIRS` for that session, and installs the
missing dependency with Houdini's hython the first time (`HFS` picks the Houdini,
`HOUDINI_EXE` what to start).

Or set it up for every Houdini session, once, with the `hython` of the Houdini to set up:

```
"C:\Program Files\Side Effects Software\Houdini 21.0.857\bin\hython.exe" usd_chunks/hosts/houdini/install.py
```

It installs usd_chunks' two pure-Python dependencies (attrs, typing-extensions) where
Houdini's Python is missing them, into the usd_chunks dependency dir, and writes
`usd_chunks.json` into the user's Houdini `packages` folder. Restart Houdini. Options:
`--chunk-dirs DIR ...` sets `USD_CHUNK_DIRS` (default: the checkout's `chunks/`), and
`--no-deps` skips the install. `usdchunks plugin-path --host houdini` prints this folder.

Houdini 21 ships attrs 22.2, older than the `>=25.4` usd_chunks declares. It is left in
place, as replacing a package Houdini itself ships is worse, and usd_chunks runs on it.

## Chunkgroups: import and export

A **chunkgroup node** (TAB > usdchunks > Core > Chunkgroup) keeps a usd_chunks network
contained: chunk nodes inside it, the group's public params as parms on it. Dive in to edit
it like any subnet. It is also the unit that travels between hosts.

**Import** -- TAB > usdchunks > **Import Network...** reads a saved network (e.g.
`swedishhouse.usda`) into the current LOP network:

- a chunkgroup becomes a chunkgroup node, and its public params parms on it;
- each chunk a chunk LOP with its saved values; a disabled one is bypassed;
- exec wires become LOP wires (several into one chunk go through a Merge LOP), and an inner
  chunk with no exec input takes the group's input;
- a param wired to another param (`@AddMesh.prim_path@`) becomes `chs("../AddMesh/prim_path")`,
  and one wired to a public param `ch("../SwedishHouse_floors")`;
- the active View becomes the group's output; with no View, the end of its chain does.

**Export** -- the chunkgroup node's **Export Network...** button writes it back as a network
file that usdview, Blender and the CLI open: the same references become param wires and
lofted params again, and the group's output its View. Only chunk nodes, nested chunkgroups
and those Merge LOPs can be inside; a Solaris node has no chunk to become, so export refuses
and names it -- put Solaris nodes upstream, feeding the group's input. A Houdini expression
other than those references is exported as its current value.

**Network space** -- a network authored in Blender or usdview is often Z-up in centimetres;
Houdini is Y-up in metres. A chunkgroup keeps the space its network was authored in (its
Network Space parms: Up Axis and Meters per Unit, set from the file on import and written
back on export). Its chunks cook in that space, so a Transform's `ty` means what it meant
where it was written, and two small Python LOPs at the group's boundary, `space_in` and
`space_out`, turn upstream content into the network's space and the result back into
Houdini's. Upstream prims come out as they went in; only what the group made is turned.
A chunk editing an upstream prim in place is turned with it.

What sets no units means a different thing on each side. A network file that sets none is
read as USD's default, centimetres (0.01). A Solaris stage that sets none is in Houdini's
Y-up metres, and the chunks cooking on it see those.

The file is written by usd_chunks' own serializer (the scripting API, `export_worker.py`),
in a process of its own under Houdini's Python, so it is exactly what Qt would save.

From a script: `chunkgroup.import_file(path, hou.node("/stage"))` and
`chunkgroup.export(node, path)`.

## How it works

| File | Role |
|---|---|
| `otls/usdchunks_chunk.hda` | The node types (expanded, text): `usdchunks::chunk`, a subnet with a Python Script LOP calling `lop.cook` and two hidden parms naming the chunk; `usdchunks::chunkgroup`, an empty subnet with the Export Network button. |
| `lop.py` | Creating, cooking, deleting a chunk node; button and menu callbacks. |
| `parms.py` | Chunk params <-> Houdini parms. |
| `scene.py` | `HoudiniScene`, the SceneSender. |
| `tools.py` | Writes and loads the TAB menu's shelf file (one tool per chunk and variant). |
| `chunkgroup.py` | The chunkgroup node: importing a network into one, exporting one. |
| `export_worker.py` | Writes an exported chunkgroup with the scripting API, in its own process. |
| `space.py` | A chunkgroup's network space: its parms, `space_in`/`space_out`, the conversion. |
| `python3.11libs/pythonrc.py` | Startup: dependency dir on `sys.path`, TAB menu once the UI is up. |
| `build_hda.py` | Regenerates the HDA (`hython build_hda.py`). Only needed when its contents change. |

A cook pushes the parms into the chunk, opens a private stage over the node's input,
runs the chunk through the same `ChunkBase._cook` every host uses, and sublayers a copy of
the chunk's layer into the node's output. What the cook changed on the chunk -- an OUTPUT
param, a group a menu hid -- is shown on the parms after the cook.

## Not yet

- Interactive chunks (`interactive=True`, authoring straight into a file layer) and chunks
  that build Qt widgets (`chunk.Custom`) have no Houdini form.
- Gizmos and the viewport camera hooks.
- Baking a Solaris node into an Inline USD chunk on export (export refuses it for now).
- A chunk needing pip packages asks to install them only when put down from the TAB menu;
  a .hip loading never stops on a dialog, and the node errors until they are installed.
