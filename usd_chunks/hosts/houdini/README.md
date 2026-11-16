# Houdini (Solaris)

Every chunk is a LOP. In a LOP network press TAB and look under **usdchunks**, where chunks
sit in their categories (Modelling, Transforms, USD, ...), or just type a chunk's name.

- **Houdini owns the graph.** Chunk nodes wire to each other and to any Solaris node with
  ordinary LOP connections; a chunk reads its input stage and authors into the active
  layer, as any Solaris node does.
  usd_chunks' own wiring (exec inputs, `@Node.param@`) is not used here -- reference another
  node's parm with Houdini's `ch("../mesh1/radius")` instead.
- **Params are native parms.** Sliders, toggles, menus, colour and vector parms, folders,
  multiparms for lists, file choosers, and Solaris's prim picker for prim path params. They
  key, take expressions and save with the .hip like any other parm.
- **Houdini decides when a chunk cooks**: a parm change or an upstream change recooks it.

Tested with Houdini 21.0.

## Setup

Install typing-extensions for Houdini's Python, then set `HOUDINI_PACKAGE_DIR` to the
`packages` folder here, which holds the package file `usd_chunks.json`. It works for every
Houdini version on Python 3.11. The full steps and fixes for common problems are in
[docs/houdini.md](../../../docs/houdini.md).

`install.py` is the older setup: it writes a package file into one Houdini version's
`packages` folder instead.

## Chunkgroups: import and export

A **chunkgroup node** (TAB > usdchunks > Core > Chunkgroup) keeps a usd_chunks network
contained: chunk nodes inside it, the group's public params as parms on it. Dive in to edit
it like any subnet. It is also the unit that travels between hosts.

**Import** -- TAB > usdchunks > **Chunk Import...** reads a saved network (e.g.
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

## Layers

A chunk node authors into Houdini's active layer, so the chunks in a row share one layer
until something starts a new one.

- **Layer** starts a new active layer and names it with its Layer Name (the node's name if
  blank). It also sets the layer's save path to `$HIP/usd/<name>.usda`, for a USD ROP.
  Houdini always puts a new layer on top, so its Insert options do nothing here.
- **Edit Target** pointing at the newest named layer, or at Follow Upstream Layer, needs
  nothing: the chunks after it already author there. Pointing at an older layer is not
  supported yet, and the node says so.
- **Export USD** lists and writes the named layers and the files in the stack. Houdini's own
  layers are left out, and so is its HoudiniLayerInfo prim, as a USD ROP drops it.

## Viewport handles

Select a chunk node and press Enter in the Scene Viewer, or pick the Handles tool. The
node's gizmos show as Houdini's own handles:

- a `box` gizmo is a **boundingbox** handle, like the Box SOP's. Drag a face and only that
  face moves. The chunk's `gizmo_changed` hook still runs, so a Blockout Box face lands on
  its Snap step;
- a `translate` gizmo is a **translate** handle. One per row works too:
  a Roof Module's placements each get one, and each drops back onto the roof.

A drag writes through `gizmo.runtime.apply_bindings`, the path usdview and Blender use,
then onto the parms. Each drag is one undo step. Inside a chunkgroup the handles are
placed in Houdini's space, after the group's `space_out`.

## How it works

| File | Role |
|---|---|
| `packages/usd_chunks.json` | The Houdini package `HOUDINI_PACKAGE_DIR` points at. Paths are relative to it, so it works from any clone. |
| `otls/usdchunks_chunk.hda` | The node types (expanded, text): `usdchunks::chunk`, a subnet with a Python Script LOP calling `lop.cook` (a Layer chunk's through a Configure Layer starting a new layer) and two hidden parms naming the chunk; `usdchunks::chunkgroup`, an empty subnet with the Export Network button. |
| `lop.py` | Creating, cooking, deleting a chunk node; button and menu callbacks. |
| `parms.py` | Chunk params <-> Houdini parms. |
| `scene.py` | `HoudiniScene`, the SceneSender. |
| `tools.py` | Writes and loads the TAB menu's shelf file (one tool per chunk and variant). |
| `chunkgroup.py` | The chunkgroup node: importing a network into one, exporting one. |
| `export_worker.py` | Writes an exported chunkgroup with the scripting API, in its own process. |
| `layers.py` | Merging a chunk's layer into the active layer; named layers, Layer and Edit Target. |
| `space.py` | A chunkgroup's network space: its parms, `space_in`/`space_out`, the conversion. |
| `viewer_state.py` | The chunk node's viewer state: pools of `boundingbox` and `translate` handles, shown per node for its gizmos. `viewer_states/usdchunks_chunk.py` registers it at startup; the HDA's DefaultState names it. |
| `gizmos.py` | Which handle each gizmo is, and the maths between a gizmo's values and a handle's parms (no `hou`, tested on its own). |
| `python3.11libs/pythonrc.py` | Startup: dependency dir on `sys.path`, TAB menu once the UI is up. |
| `build_hda.py` | Regenerates the HDA (`hython build_hda.py`). Only needed when its contents change. |

A cook pushes the parms into the chunk, opens a private stage over the node's input,
runs the chunk through the same `ChunkBase._cook` every host uses, and merges the chunk's
layer into the active layer, the chunk's edits winning. What the cook changed on the chunk -- an OUTPUT
param, a group a menu hid -- is shown on the parms after the cook.

## Not yet

- Interactive chunks (`interactive=True`, authoring straight into a file layer) and chunks
  that build Qt widgets (`chunk.Custom`) have no Houdini form.
- Gizmos other than `box` and `translate` (see Viewport handles), and the viewport camera
  hooks.
- Edit Target pointing at an older layer than the newest; Mute Layer (it mutes on the
  node's own stage only, so nothing after it sees the mute).
- Baking a Solaris node into an Inline USD chunk on export (export refuses it for now).
- A chunk needing pip packages asks to install them only when put down from the TAB menu;
  a .hip loading never stops on a dialog, and the node errors until they are installed.
