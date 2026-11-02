# Undo and redo

usd_chunks undoes **what chunks author**, on any host, in pure Python on the host's own
`pxr`. It does not undo anything else.

## What is undone

- **Chunk prims**: every prim the stage says is a `Chunk` -- its params, wires, view,
  position and metadata. Adding and deleting chunks, too.
- **Backdrops and sticky notes** in a chunk graph: moving, resizing, recolouring, editing
  their text, adding and deleting them.
- **Wherever they are authored in the stage's own layers**: the root layer, the session
  layer, department sublayers the Edit Target picked, and overs on the prims a Reference
  chunk brought in.
- Cooked output is not recorded: it lives in each chunk's own layer, and cooking after an
  undo makes it again.

## What is not undone

- **Prims that are not chunks**: scene data authored directly, edits by other plugins or
  by scripts writing to the stage.
- **Layers outside the stage's own layer stack**, such as a referenced asset file edited
  in place.
- **Layer-level changes**: adding, removing or reordering a sublayer, or layer metadata
  (`defaultPrim`, `upAxis`).
- Chunk prims nested inside variant sets.

**If you need those undone, use the C++ [usd-undo](https://github.com/HuginStudios/usd-undo)
plugin, or your host's own USD undo.** usd-undo records every field edit on any layer it is
attached to; it has to be built against your host's USD.

## Per host

| host | how |
|---|---|
| Blender | Ctrl+Z / Ctrl+Shift+Z, as part of Blender's own undo. Opening a network is an undo step. |
| usdview | Edit > Undo / Redo (Ctrl+Z / Ctrl+Shift+Z). If the usd-undo plug-in is loaded, it owns those keys and usd_chunks adds none -- two actions on one shortcut make Qt fire neither. |
| Flet, ImGui | Not yet. |

In usdview a step runs from one user input to the next: a slider drag or a gizmo drag is
one undo, and a cook that lands after an edit belongs to that edit's step.

## For host authors

`usd_chunks.chunk_undo.History` records a stage. A host with its own undo stack stores
`history.key()` with each of its undo steps and calls `history.move_to(key)` after an undo
or redo (see `frontends/blender/addon/undo.py`). A host without one uses
`chunk_undo.UndoStack` and calls `note_input()` on each user input (see
`frontends/qt/undo.py`, which does it for any Qt app).
