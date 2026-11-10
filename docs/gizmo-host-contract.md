# Chunk gizmos: the host contract

How a DCC displays chunk-driven viewport handles.

**usd_chunks records. The DCC interprets.** This repo authors a schema onto chunk prims
and owns the write-back. It does not draw, pick, or drag. If you are building a viewport
layer — `usd-guides` for usdview and Kraken, or a native integration for Houdini, Blender
or Maya — this page is the whole surface you need.

The governing rule everything here is designed around:

> A host must be able to build a native handle from `ui:gizmo:*` alone, with no geometry
> library from us.

Three of the five target hosts have manipulators better than anything we would ship, so
our geometry would be a downgrade there. Nothing below asks you to draw our shapes.

---

## The four calls

```python
chunk_gizmo.resolve_gizmos(prim) -> list[GizmoDesc]   # what handles exist
chunk_gizmo.apply_bindings(chunk_prim, name, value)   # write-back
chunk.start_gizmo() / chunk.stop_gizmo()              # drag bracket
```

Plus two optional helpers:

```python
chunk_gizmo.decompose(matrix) -> {"translate": Vec3d, "rotate": Vec3d, "scale": Vec3d}
chunk_gizmo.ensure_transform_chunk(prim) -> GizmoDesc | None   # see §8
```

That is all of it. There is no callback to register, no base class to subclass, and
nothing we call on you.

---

## 1. Discovery: from a selected prim to its handles

The user selects `/World/Cube`. The handles are declared on a chunk prim under `/chunks`,
which is not in the viewport and is not what anyone clicks. Two routes:

**Route A — ask us.**

```python
for desc in chunk_gizmo.resolve_gizmos(selected_prim):
    ...
```

Walks the prim's layer stack. Every chunk owns its own `Sdf.Layer`, so a prim's spec stack
already records which chunks authored it. Cheap, and a disabled or muted chunk drops out
for free because its specs are not in the stack.

Also accepts a chunk prim directly, for a node-graph-driven UI.

**Route B — pure USD, no dependency on this repo.**

Index every prim carrying a `ui:gizmo:*:anchor` relationship, and find the ones whose
targets include your selection. Slower — O(chunks) per selection change, which is nothing
at realistic graph sizes — but it means a third-party host is never *forced* to import
usd_chunks just to find handles.

Write-back still needs us. See §5.

> **A handle only appears after the chunk has cooked at least once.** Route A traces
> through the layer that a cook writes; before that the layer is empty and there is
> nothing to trace. Chunks cook on creation, so this is rarely visible — but if handles
> are missing on a freshly loaded scene, this is why.

**Order is meaningful: nearest the view first.** `resolve_gizmos` returns handles sorted by
exec depth, so `descs[0]` is the chunk with the final say over that prim. A host with no
chunk selected can drive `descs[0]` and be editing the one the user can actually see.

Sorted by the graph rather than by layer strength, because the two disagree in one case
that matters: a chunk which has not authored anything yet is not in the prim's layer stack
at all. An Offset Transform sitting at zero authors nothing by design, so it would sort
last however far downstream it sat -- and ownership would flip the moment someone typed a
number into it.

**Which handle belongs to whom.** `resolve_gizmos` returns handles from *every* chunk in
the prim's layer stack, upstream ones included. **You filter to the chunk the user has
selected**, if your UI has a notion of one. A prim touched by two transform chunks in one chain would otherwise show two
identical handles in the same place. Stated the other way round: a handle only ever edits
the chunk you are looking at.

---

## 2. The record

`GizmoDesc` is a plain data record. No Qt types, no callbacks, nothing beyond `pxr`.

| field | type | meaning |
| --- | --- | --- |
| `name` | `str` | Identity within the chunk. The namespace segment, never the type — two cubes on one chunk is ordinary. |
| `type` | `str` | What kind of handle, and what it sends back. See §3. |
| `label` | `str` | Display name. Falls back to the chunk's name. |
| `chunk_prim` | `Sdf.Path` | Which chunk declared this handle. **Pass it straight back to `apply_bindings`**, and use it to filter to the chunk the user selected. `name` is not unique across chunks — more than one calls its handle `transform`. |
| `anchor` | `tuple[Sdf.Path]` | The scene prims the handle hangs off. Already resolved — never a param to evaluate. |
| `anchor_source` | `str` | The param that produced them. For our write-back; you do not need it. |
| `driver` | `tuple[Sdf.Path]` | The properties this handle writes, in order. Informational — write through `apply_bindings`, not directly. |
| `axis` | `str \| None` | Signed direction token, in the anchor's local space. `None` means free drag. |
| `space` | `str` | `"local"` or `"world"`. **Defaults to `"local"` and is only authored when non-default.** |
| `editmode` | `tuple[str]` | When to show it. See §4. |
| `offset` | `tuple[float,float,float] \| None` | Where the handle sits, as a point in the anchor's own space. `None` means **you decide** — put it on the geometry. Not static: a chunk can move it between resolves. |
| `size` | `float \| None` | Relative size hint. **`None` means you decide** — usually the right answer, since a viewport wants a screen-constant handle and only you know the camera distance. |

A chunk can suppress one of its own handles while a mode of its own would fight it —
camera.py hides its transform handle while `lock_to_freecam` is on, because the freecam
re-authors the transform every frame and a drag would silently snap back. **`resolve_gizmos`
simply does not return a suppressed handle**, so route A needs no check. Route B must
honour `ui:gizmo:<name>:hidden = true`, which is authored for exactly this purpose.

**Several anchor targets means one handle at their averaged pivot**, not one handle per
target. A transform chunk pointed at three prims is ordinary use.

### Where to draw it

The anchor says *what the handle is attached to*. It is not always where the handle sits.

**Read the driven value and offset by it.** Follow the `driver` connections, read the
current values, and place the handle accordingly:

- A `translate` handle driving an **offset** — "shift this body 3 units from its start" —
  belongs at `anchor + (3, 0, 0)`. Drawn on the anchor it would sit at zero and never move.
- A `cube` on **focus distance** with `axis = "-Z"` belongs at the focus point, `distance`
  along the camera's -Z. That is where the existing focus reticle already draws.

The rule is the same in both cases: the handle shows *the value*, and the anchor is the
frame that value is measured in. `offset` is a point on top of that — not a substitute for
reading the value.

**`offset` is a pivot, and it is not static.** A Transform chunk with a pivot set reports it
here, so:

- `offset is None` — no chunk has an opinion. **Place the handle yourself**, and put it on
  the geometry rather than on the transform origin: those differ more often than they look
  like they should. A `Kitchen_set` cup carries a transform on its group and very nearly the
  inverse on the mesh, so its world transform composes to the identity and its origin is out
  at the world origin. The bbox centroid is the answer that stays useful.
- `offset` set — draw there, and make your rotation turn about it. It is a point in the
  anchor's **own object space**, so map it through the anchor's local-to-world:
  `primWorld.Transform(offset)`. The chunk's op is the innermost one, so everything upstream
  of it still applies to the pivot.

The value moves *while the user drags a pivot slider*, so re-read it on stage change rather
than caching it at selection time. Nothing else about the drag changes: you still report the
absolute matrix the user dragged to, and the chunk solves its own params about its own pivot.

### Letting the user drag the pivot

Optional, and the reason `offset` has a write counterpart. Hold a modifier (usdview's gizmo
uses `D`, matching its Maya-ish `W`/`E`/`R`/`M`) and a drag moves the pivot rather than the
object:

```python
chunk_gizmo.apply_offset(desc.chunk_prim, desc.name, world_point)   # -> bool
```

**You send a world point.** You have a position under the mouse and nothing else; you do not
know that a Transform chunk keeps its pivot in the anchor's object space, and you should not
have to. The conversion happens on our side, exactly as `apply_bindings` takes an absolute
matrix and lets the chunk work out its own share.

Three rules:

- **False means do nothing.** Unlike `commit_transform`, do *not* fall back to writing the
  prim — there is nowhere on a prim to put a pivot, and moving the prim instead is the
  opposite of what was asked. False just means no chunk here owns a pivot.
- **Write every tick, never batch to mouse-release.** The chunk cooks per write, and that is
  what keeps the object still while the pivot moves under it. Same rule as every other drag.
- **Reset your handle when the gesture ends.** A pivot drag leaves your manipulator holding
  the whole drag in its local translation; carry that into the next ordinary drag and the
  object jumps by however far the pivot moved.

A chunk that does not implement the write hook returns False, so this degrades to "no pivot
dragging" rather than to anything surprising.

---

## 3. Types, and what each one sends back

**A gizmo drives exactly one thing, and its type says what.** There is no slot, no key, no
payload map — reading the type answers the whole question.

| `type` | you send back | drives |
| --- | --- | --- |
| `transform` | `Gf.Matrix4d` | Nine params: translate xyz, rotate xyz, scale xyz |
| `translate` | `Gf.Vec3d` | Three params, or one vector-typed param |
| `rotate` | `Gf.Vec3d`, Euler XYZ, **degrees** | as above |
| `scale` | `Gf.Vec3d` | as above |
| `cube`, `sphere` | a single number | One param |
| `box` | six numbers | Six params: the +X, -X, +Y, -Y, +Z, -Z faces, in that order |

**`transform` is the whole-chunk handle** — in effect *"run your native transform
manipulator and give me the result"*. Build your own TRS handle, let the user drag it, and
hand back the matrix it produced. You never need to decompose it; we do that, so the Euler
convention lives in exactly one place instead of being re-derived slightly differently in
every host.

**Shape types are one-dimensional handles.** A shape plus an `axis` *is* a linear handle:
map it onto your native linear-handle primitive and ignore the shape name entirely.
`usd-guides` draws the shape literally; Houdini and Blender should not.

**`box` is a box whose faces are the handles.** Draw no handle of your own. Each driven
param is the distance from the anchor's origin to one face, in the anchor's local space, so
the six values *are* the box. The user hovers a face, it highlights, they grab it and pull
it along its normal; only that face moves. Two pure helpers do the maths, in the anchor's
space (apply the inverse of its world transform to the mouse ray first):

```python
hit = chunk_gizmo.pick_box_face(values, origin, direction)  # on hover and press
if hit:
    face, t = hit
    start, grab = values, origin + direction * t            # keep both for the drag
...
new = chunk_gizmo.drag_box_face(start, face, grab, origin, direction)  # every tick
chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, new)
```

**Pick against the box, never the scene.** `pick_box_face` tests the box the six values
describe, so a mesh inside the volume can never be what is grabbed. Test it before your
own pick and let a hit win. `drag_box_face` always starts from the press's values and grab
point, not the last tick's, so a long drag does not drift. Snapping is the chunk's
(`gizmo_changed`), not yours.

**Unknown types.** The set will grow. Skip a `type` you do not recognise rather than
guessing — a *custom* type is one only the `usd-guides` render path understands, and
silently mis-drawing it is worse than not drawing it.

### `axis`

`X`, `-X`, `Y`, `-Y`, `Z`, `-Z`, `screen` — in the **anchor's local space**.

**The sign is load-bearing.** Camera focus distance is `-Z`, because a camera looks down
-Z. An unsigned token would put that handle out the back of the camera.

`None` (attribute absent) means free drag: any mouse direction moves the driven value, and
you choose the mapping. Sensitivity needs no schema — numeric params carry `min_value` /
`max_value`, so scale screen delta against the declared range.

Resolve the token to a world direction from the anchor's matrix, which you already hold in
order to draw. Deliberately not a vector: one authored in world space would be wrong the
moment the anchor rotates.

---

## 4. When to show a handle: `editmode`

A `token[]`, because a handle wanting more than one is normal.

| value | show it when |
| --- | --- |
| `onPrimSelection` | your viewport selection includes the anchor prim |
| `onChunkSelection` | the chunk itself is selected — node graph, or a LOP node |
| `always` | the chunk is live |

Defaults to `["onPrimSelection"]`.

You apply this against a selection **you already own**. Nothing pushes at you and there is
no activation call to implement. `onChunkSelection` is defined but not yet wired on our
side — treat it as reserved.

---

## 5. Write-back

```python
chunk.start_gizmo()
while dragging:
    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, value)   # every tick
chunk.stop_gizmo()
```

`apply_bindings` takes either the chunk prim path off the record or a live chunk object, so
a host that only ever holds `GizmoDesc` needs no lookup of its own.

### Do not write the prim directly

It will be erased. A chunk like `transform.py` clears its own layer and re-authors
`xformOp:transform` on **every cook**, so a direct write survives until the next execute
and then vanishes. Nor should you write the `inputs:` attributes in `driver` yourself —
those are param storage, and going around the param system skips the propagation that
makes downstream chunks update.

`apply_bindings` writes the params, which dirties the chunk, which recooks, which moves the
prim. That is the whole reason this boundary exists.

### Write on every tick

> **Never batch writes to `stop_gizmo()`.**

This is the tempting optimisation the first time a heavy chunk cooks slowly, and it is
exactly what makes a handle feel dead — the object stops following the cursor and jumps at
the end. Writing per tick is already how sliders behave. If a chunk is too slow to drag,
fix the cook.

### Notices

`GIZMO_START` → `GIZMO_CHANGED` (one per tick, after the write) → `GIZMO_STOP`.

If your host has its own undo stack, open a block on START and close it on STOP. usdview
needs none of this: `chunk_undo.UndoStack` (fed by `frontends/qt/undo.py`) makes one step
of everything from a mouse press to the next input, so a drag is one undo whether its
writes came from a handle, a slider or a cook. It undoes chunk prims only
(docs/undo.md); with the C++ `usd-undo` plugin loaded, that one owns Ctrl+Z instead and
coalesces at `Sdf` level, as it does in Kraken.

### Errors

- An unknown gizmo name raises — you asked for a handle the chunk does not declare.
- A wrong-arity value raises **before writing anything**, so a bad drag cannot leave a
  chunk half-updated.
- Some chunks solve their own drags (a lookat point has no param to bind to). This is
  invisible to you: same call, same payload.

---

## 6. Conventions not inferable from the USD

Three things you cannot read off the attributes. They are here because a host that guesses
gets them silently wrong.

1. **The `transform` driver is ordered translate, rotate, scale.** Nine connections. The
   arity is discoverable by counting; the order is not. Only matters if you bypass
   `apply_bindings`, which you should not.
2. **Absent `space` means `local`.** Only authored when non-default, to keep the usda
   quiet.
3. **Absent `size` means you decide.** Not "size 1".

---

## 7. Graceful degradation

A DCC that has never heard of `ui:gizmo:` ignores the attributes completely. They are
plain `uniform token` attributes and one relationship on a prim it already ignores. There
is no plugin to register and nothing breaks.

---

## 8. A prim no chunk drives

`resolve_gizmos` returns nothing for a prim that no chunk touches — an imported mesh, a
cube from a sublayer. The obvious fallback is to write the prim directly, and it is the
wrong one: that edit is not in the graph, so it does not survive the driving chunk's next
cook and it is not what gets written out when the scene is saved. The user moved something
and nothing recorded it.

```python
desc = chunk_gizmo.ensure_transform_chunk(prim)   # creates one if nothing offers a handle
if desc:
    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, matrix)
```

Same idea as Houdini dropping an Edit node in front of geometry you start moving. We decide
what to create and where it lands in the graph, so a host never learns the `/chunks` layout:

- **Only when nothing already places the prim.** The test is `placement_gizmos(prim)` —
  any handle of type `transform`, `translate`, `rotate` or `scale`. Not "is there a
  Transform chunk", and deliberately not "is there a `transform` handle" either: rbd_solver
  declares separate `translate` and `rotate` offsets and no `scale`, so it can never form a
  whole-chunk transform, yet it plainly owns where that body starts. Testing only for
  `transform` reported such a prim as unowned and stacked a duplicate chunk on top of it.
  Declaring any placement handle is how a chunk opts out.
- **A prim can be owned without offering one matrix-taking handle.** `ensure_transform_chunk`
  then returns `None` having created nothing — that is "owned by parts", not "unowned".
  Drive the parts individually: `decompose(matrix)` keys by gizmo *type*, so each entry goes
  to the handle of that type, and a type with no handle is simply not written.
- **At the end of the active context's chain**, wired to the tail, with any view moved
  along to it — the same placement as creating it by hand in the Params tab.
- **Seeded from the prim's current local transform**, so the object does not jump when the
  chunk appears.
- **Idempotent.** Safe to call once per mouse-move; the second call resolves the chunk the
  first one made.
- Returns `None` if it could not create one (no `transform` module, no live scene). Decide
  for yourself whether that is fatal or a reason to fall back to writing the prim.

Call it at grab time if you have a drag-start hook, so the creation lands inside your undo
bracket. Calling it on the first commit works too, one frame later.

---

## Worked example

Selecting `/World/Cube`, which a Transform chunk drives:

```usda
uniform token   ui:gizmo:transform:type     = "transform"
uniform token   ui:gizmo:transform:label    = "Transform"
uniform token[] ui:gizmo:transform:editmode = ["onPrimSelection"]
rel             ui:gizmo:transform:anchor   = </World/Cube>
uniform token   ui:gizmo:transform:driver.connect = [ ...nine params... ]
```

A Houdini integration, with no geometry from us:

1. Selection changes → `resolve_gizmos(selected_prim)` (or the anchor-rel index).
2. Filter to the selected chunk.
3. `editmode` contains `onPrimSelection` and the anchor matches → show it.
4. `type == "transform"` → build the native TRS handle; `label` names it.
5. Place it at the anchor prim's world transform. Several anchors → their averaged pivot.
6. Press → `chunk.start_gizmo()`, open an undo block.
7. Each drag tick → `apply_bindings(chunk, "transform", handle_matrix)`.
8. Release → `chunk.stop_gizmo()`, close the block.

The user sees the cube follow the handle, because step 7 wrote nine float params and the
chunk recooked.
