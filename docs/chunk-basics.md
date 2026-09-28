# Chunks, params and groups

A **Chunk** is a container of parameters, groups and attributes — the params hold the
values you actually interact with. Change anything on a chunk and it sends a notice;
callbacks (plain Python functions) subscribe to those to do the real work, usually
authoring to the stage. That's what makes a chunk dynamic: a callback can add or remove
params, flip flags, or change what the chunk does next, all at runtime.

## A minimal example

A button that creates a cube, one click at a time:

```py
from usd_chunks import chunk
from pxr import Usd


def addCube(owner, notice, sender):
    stage = owner.get_stage()
    count = 0
    while True:
        prim = stage.GetPrimAtPath(f"/cube{count}")
        if not prim.IsValid():
            prim = stage.DefinePrim(f"/cube{count}", "Cube")
            break
        count = count + 1


def main():
    button = chunk.Button("add_cube", "Add Cube")
    button.add_callback(addCube)

    chunk_api = chunk.Chunk("Add Cubes")
    chunk_api.add_param(button)

    return chunk_api


def config():
    return chunk.DefaultConfig(category=chunk.StandardCategory.EXAMPLES)
```

![image](assets/example_add_cube.png)

That's the whole shape: `main()` builds the chunk and its params, `config()` sets where it
files in the create menu. See [Writing a chunk](writing-a-chunk.md) for the full picture —
`main()`, `config()`, the reactive `execute()` hook, and the file-writing `commit()` hook —
and [chunks/](https://github.com/HuginStudios/usd_chunks/tree/main/chunks) for more
worked examples.

## 🎛️ Params

Params are the leaves — each one maps to a widget in the UI and, once its value differs
from the default, to an `inputs:` attribute on the chunk's prim.

| Param | Value | Notes |
| --- | --- | --- |
| `Slider` | `float` \| `int` | `min_value`, `max_value`, `is_int=True` for integers |
| `Menu` | `str` | `menu_items` is the list of choices |
| `Checkbox` | `bool` | |
| `LineEdit` | `str` | Single line |
| `TextEdit` | `str` | Multi-line; `rows` sets the height |
| `FileBrowser` | `str` \| `Sdf.AssetPath` | `extensions` and `pattern` filter the dialog |
| `ColorPicker` | `(r, g, b)` | Components in the 0–1 range |
| `Button` | — | Holds no value; use it with a callback |
| `Label` | `str` | Display only, not editable |
| `Separator` | — | Visual grouping only |
| `Custom` | any | For a custom widget |

```py
divisions = chunk.Slider("divisions", "Divisions", min_value=1, max_value=32, is_int=True)
mode = chunk.Menu("mode", menu_items=["linear", "smooth"])

divisions.value = 8   # read/write through .value
```

Prefer `Slider` over `Spinbox` for numeric values — `Slider` covers the integer case with
`is_int=True`, and is the type the rest of the toolkit is built and tested against.

**Param flags** (`ChunkFlags.ParamFlags`) adjust behavior:

- `OUTPUT` — a computed result. Hidden from the UI, but still readable by other chunks as
  `@ChunkName.param@`.
- `LABELED` — draw a label next to the widget.
- `FILE_EXISTING`, `DIRECTORY` — for `FileBrowser`: restrict to existing files, or folders.
- `PRIM_SELECTION`, `PROP_SELECTION` — for `LineEdit`: the value is a prim or property
  path, so the UI can offer selection.
- `PY_SYNTAX_HIGHLIGHT`, `USD_SYNTAX_HIGHLIGHT` — for `TextEdit`.

**State** (`ChunkFlags.State`) carries the current condition instead: `HIDDEN`,
`DISABLED`, `COLLAPSED`, `SELECTED`, and `STALE` (a UI hint that a result no longer
reflects its inputs — nothing sets or clears this automatically).

## 📁 Groups

Groups hold params and other groups, giving a chunk its structure. A `Chunk` is itself a
container, so `add_param`, `add_params`, `get_param`, `get_my_params` work at every level.

- `Group` — a plain container.
- `ListGroup` — children can be added and removed at runtime, for variable-length input.
- `Vector` — a fixed set of params treated as one value, for something like an XYZ triple.
  Expressions can reference the whole vector as `@Node.vecParam@`.

```py
transform = chunk.Group("transform", "Transform")
transform.add_params([
    chunk.Slider("tx", min_value=-100, max_value=100),
    chunk.Slider("ty", min_value=-100, max_value=100),
])
chunk_api.add_param(transform)
```

`ChunkFlags.GroupFlags` controls presentation: `LABELED`, `COLLAPSABLE`, `FRAMED`, `LIST`,
and `CONTEXT`. `CONTEXT` is the one worth knowing early — a context group renders as a
workspace holding other chunks rather than as a node itself (a tab, in the Qt plugin).
[View and ChunkGroup](core-chunks.md) are both built on it.
