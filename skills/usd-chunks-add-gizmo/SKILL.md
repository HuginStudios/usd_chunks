---
name: usd-chunks-add-gizmo
description: Add viewport handles (gizmos) to a usd_chunks chunk -- the gizmo= shorthand on params and groups, or chunk.Gizmo declarations in config() -- plus the optional gizmo_* hooks and a test that drives the handle. Use when asked for a gizmo, handle, manipulator or drag control in the viewport for a chunk's params.
---

# Add a gizmo to a chunk

A gizmo is a viewport handle that drives a chunk's params. **usd_chunks only records it**:
the chunk declares it, the framework writes it on the chunk prim as `ui:gizmo:*` and owns
the write-back. The host (usdview through `usd_gizmos`, Houdini, Blender) draws it and
sends values back. So a chunk never draws anything and never imports a viewport library.
The host side is `docs/gizmo-host-contract.md`; the declaration is
`usd_chunks/gizmo/types.py`.

## 1. Pick the handle type

A gizmo drives exactly one thing, and its type says what it sends back:

| `type` | Goes on | Sends back |
| --- | --- | --- |
| `translate`, `rotate`, `scale` | a `Vector` or a group of three params | `Gf.Vec3d` (rotate: Euler XYZ, degrees) |
| `cube`, `sphere` | one scalar param (a `Slider`) | a number |
| `transform` | the whole chunk; drives nine params, translate xyz, rotate xyz, scale xyz | `Gf.Matrix4d` |
| `box` | six face params (+X, -X, +Y, -Y, +Z, -Z), declared in `config()` | six numbers; the box's faces are the handles, dragged one at a time |
| `brush` | a painting chunk (see `chunks/modelling/scatter_brush.py`) | stroke samples to a hook |

Read a chunk that already does it: `chunks/transforms/transform.py` (groups + a whole-chunk
`transform`), `chunks/rigs/skel_pose.py` and `chunks/rigs/ik_target.py` (`rotate` /
`translate` on vectors), `chunks/rigs/pose_articulation.py` (`cube` on sliders), and
`chunks/modelling/camera.py` (a full declaration with an axis).

## 2. Declare it

**The shorthand**, for the common case: `gizmo=` on the param or group it drives.

```python
offset = chunk.Group("offset_grp", "Offset", gizmo="translate")      # drives its three params
offset.add_params([chunk.Slider(n, n.upper(), min_value=-100.0, max_value=100.0)
                   for n in ("ox", "oy", "oz")])

height = chunk.Slider("height", "Height", min_value=0.0, max_value=10.0, gizmo="cube")
```

The wrong kind raises when the chunk loads: `gizmo="translate"` on a single `Slider`, or
`gizmo="cube"` on a group. `chunk.Chunk("Name", gizmo="transform")` gives the whole chunk
one native manipulator and absorbs its translate/rotate/scale group handles.

**A full declaration** in `config()`, when the handle needs more than a type: a direction,
a named anchor, a label, or a driver that isn't one group.

```python
def config():
    return chunk.DefaultConfig(
        gizmos=[
            chunk.Gizmo("zdist", type="cube", label="Focus Distance",
                        axis="-Z", driver="focusDistance"),
        ],
        ...
    )
```

| Field | Meaning |
| --- | --- |
| `name` | The handle's id on this chunk (not its type; two cubes on one chunk is fine) |
| `type` | From the table above |
| `driver` | The param path(s) it writes: one (`"focusDistance"`, a vector), three, or nine for `transform`, in order. Required |
| `anchor` | Name of the prim-path param the handle hangs off. Empty: inferred from the `PRIM_SELECTION` param, or `primpath_location` |
| `axis` | `X`, `-X`, `Y`, `-Y`, `Z`, `-Z` or `screen`, in the anchor's local space. Set it only when the direction is the meaning (focus runs along -Z). `None` = free drag |
| `label` | Display name; defaults to the chunk's name |
| `space` | `"local"` (default) or `"world"` |
| `editmode` | When to show it: `onPrimSelection` (default), `onChunkSelection`, `always` |
| `offset`, `size` | A placement point in the anchor's space, and a size hint. Leave both `None` unless you need them: the host places and sizes handles better |

A bad declaration (unknown type or axis, missing driver, `transform` without nine
drivers) raises a `ValueError` at load.

## 3. Optional hooks

Module-level functions the framework calls if they exist. Write them only when needed:

- `gizmo_hidden(node, gizmo) -> bool`: hide a handle while a mode of the chunk would fight
  it. Camera hides its transform while Lock to Free Camera is on.
- `gizmo_offset(node, gizmo) -> tuple | None`: where the handle sits right now, such as a
  pivot. `None` = no opinion.
- `gizmo_changed(node, gizmo, value) -> bool`: solve the drag yourself (IK, constraints).
  Set the params, then return `True` to skip the declared drivers.
- `gizmo_stroke(node, gizmo, samples) -> bool`: for `brush` handles.
- `gizmo_offset_changed`: for a draggable pivot. Read its use in `chunks/transforms/transform.py`
  and `usd_chunks/gizmo/runtime.py` before you write one.

A hook sets params; the cook authors from them. Never author USD in a hook.

## 4. Test it

Drive the handle the way a host does: find it from the prim, write a value back, and check
the params and the stage. A handle only appears after the chunk has cooked once, which the
Scene API does on create.

```python
from __future__ import annotations

from pxr import Gf, Usd, UsdGeom

from usd_chunks.chunk_script import Scene
from usd_chunks.gizmo import runtime as chunk_gizmo


def test_dragging_the_handle_moves_the_prim():
    s = Scene(Usd.Stage.CreateInMemory(), "pytest")
    try:
        box = s.create.cube(prim_path="/geo/box")
        lift = s.create.transform(box, primpath_location=box.get_param("prim_path"))
        prim = s.get_stage().GetPrimAtPath("/geo/box")

        (handle,) = chunk_gizmo.resolve_gizmos(prim)
        assert handle.type == "transform"

        chunk_gizmo.apply_bindings(lift, handle.name, Gf.Matrix4d().SetTranslate(Gf.Vec3d(1, 2, 3)))

        assert (lift.get_param("tx").value, lift.get_param("ty").value) == (1.0, 2.0)
        world = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0).ExtractTranslation()
        assert world == Gf.Vec3d(1, 2, 3)
    finally:
        s.close()
```

`resolve_gizmos` lists the handles of every chunk that authored the prim, nearest the view
first. Filter by `handle.chunk_prim` when more than one chunk touches it. Also test a hook
if you wrote one: `gizmo_hidden` makes `resolve_gizmos` leave the handle out, and
`gizmo_changed` sets the params you expect. Existing examples are
`tests/test_chunk_gizmo_*.py`.

## 5. Check it

```
USD_CHUNK_DIRS=chunks pytest -n0 tests/test_<module>.py
ruff check <module.py>
```

To see it in usdview, `usd_gizmos` must be installed. Without it the handles are recorded
but not drawn, and nothing breaks. Tell the user which handles you added, what each one
drives, and how you tested them.
