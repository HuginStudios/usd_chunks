"""Blender's viewport handles for chunk-driven prims.

The host half of `docs/gizmo-host-contract.md`. usd_chunks records `ui:gizmo:*` on a chunk
prim and owns the write-back; this file is Blender's interpretation of that record: which
handles exist for what the user picked (`chunk_gizmo.resolve_gizmos`), where they sit, and
the per-tick `chunk_gizmo.apply_bindings` that moves the prim by moving the chunk's params.

**Native handles, not ours.** The contract's governing rule is that a host builds its own
manipulator from the record alone, and Blender's gizmos are better than anything we would
ship. A `transform` desc -- "run your native transform manipulator and give me the
result" -- becomes three arrows, three dials and three box-arrows here, because Blender has
no single instantiable TRS gizmo (`VIEW3D_GGT_xform_gizmo` is C code bound to objects, and
a USD prim drawn by the Hydra delegate has no object to bind). Each handle writes the one
component it owns, through one matrix, so the chunk still decomposes exactly once.

**Why custom Gizmo subclasses rather than `GIZMO_GT_*` built-ins.** The built-ins draw and
pick perfectly well, but a drag through them is a sequence of `set` calls with no start and
no end, and the contract wants `start_gizmo()` / `stop_gizmo()` around a drag -- that
bracket is what a host's undo stack hangs off, and what tells a chunk a gesture is in
progress. `invoke` and `exit` exist only on a subclass, so the handles are subclasses that
draw a preset shape.

**Picked, not selected.** A prim here comes from viewport picking (viewport.py's
`USDCHUNKS_OT_pick_prim`), since a Hydra-drawn prim is not a Blender object and cannot be
in `context.selected_objects`. That is the selection `editmode = onPrimSelection` is
matched against.
"""
from __future__ import annotations

import logging

import bpy
from mathutils import Matrix
from pxr import Gf, UsdGeom

from usd_chunks import chunk_gizmo
from usd_chunks.plugins import chunk_blender_nodegraph as bg
from usd_chunks.plugins.chunk_blender_addon import viewport

logger = logging.getLogger(__name__)

#: Blender's axis colours, so a chunk handle reads like every other handle in the viewport.
_AXIS_COLOR = {0: (1.0, 0.2, 0.32), 1: (0.54, 0.86, 0.0), 2: (0.16, 0.52, 1.0)}

#: Which component each handle drives, and the order the contract gives for a `transform`
#: driver: translate, rotate, scale.
_TRANSLATE, _ROTATE, _SCALE = "translate", "rotate", "scale"

#: Degrees per radian, spelled once. A `rotate` value goes back as Euler XYZ in DEGREES
#: (contract §3) while Blender works in radians, and this is the one place that converts.
_DEG = 57.29577951308232


def _active_view():
    """The add-on's live graph view, or None.

    Imported here rather than at module scope: the add-on package imports this module in
    order to register it, so a module-scope import back into it is a cycle.
    """
    from usd_chunks.plugins import chunk_blender_addon as addon
    return addon.get_view()


def _picked_prim(stage):
    """The prim a handle should hang off, or None.

    The first picked prim, not all of them: a handle drives one anchor, and
    `resolve_gizmos` already averages several anchors into one pivot when a chunk points at
    several prims. Picking two unrelated prims is not a request for a combined handle.

    Walks up from what was clicked to the nearest ancestor a chunk actually drives.
    `chunks_for_prim` reads a prim's OWN spec stack, and a click lands on a gprim -- the
    Mesh -- while a Transform chunk authors its xformOp on the Xform above it. Without the
    walk, clicking the thing you can see finds no handle at all.
    """
    if stage is None:
        return None
    for path in bg.get_picked_prim_paths():
        prim = stage.GetPrimAtPath(path)
        while prim and prim.IsValid() and not prim.IsPseudoRoot():
            if chunk_gizmo.resolve_gizmos(prim):
                return prim
            prim = prim.GetParent()
    # Nothing on the click's ancestry is driven. The clicked prim itself is still the
    # answer -- ensure_transform_chunk turns it into something driveable on first drag.
    for path in bg.get_picked_prim_paths():
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid():
            return prim
    return None


def _visible_descs(prim, view):
    """The handles to show for this prim: the ones whose editmode matches a viewport
    selection, filtered to the chunk the node graph has selected when it has one.

    The filter is the contract's rule in §1 -- a prim touched by two transform chunks in
    one chain would otherwise show two identical handles in the same place, and a handle
    must only ever edit the chunk you are looking at. With nothing selected in the graph,
    `descs[0]` is the chunk nearest the view, which is the one the user can actually see.
    """
    descs = [d for d in chunk_gizmo.resolve_gizmos(prim)
             if "onPrimSelection" in d.editmode or "always" in d.editmode]
    if not descs:
        return []
    selected = set(getattr(view, "selected_nodes", ()) or ())
    if selected:
        owned = [d for d in descs if str(d.chunk_prim) in selected]
        if owned:
            return owned
    return [d for d in descs if d.chunk_prim == descs[0].chunk_prim]


def _anchor_matrix(stage, desc) -> Matrix:
    """Where to draw, as a Blender matrix: the anchor's local-to-world, with the chunk's
    pivot applied when it has an opinion about one.

    `offset` is a point in the anchor's OWN object space (contract §2), so it maps through
    the anchor's local-to-world rather than being added in world space. `None` means the
    chunk has no opinion and the host decides -- the anchor's own origin is the answer
    here, since Blender's handles are drawn at a matrix and the bbox centroid the contract
    suggests would need a bound computation per redraw.
    """
    identity = Matrix()
    anchors = [stage.GetPrimAtPath(p) for p in desc.anchor]
    anchors = [a for a in anchors if a and a.IsValid()]
    if not anchors:
        return identity
    xf = UsdGeom.Xformable(anchors[0])
    if not xf:
        return identity
    gf = xf.ComputeLocalToWorldTransform(0)
    if desc.offset is not None:
        gf = Gf.Matrix4d().SetTranslate(Gf.Vec3d(*desc.offset)) * gf
    rows = [[gf[r][c] for c in range(4)] for r in range(4)]
    # Gf is row-major with translation in row 3; Blender is column-major. Transposing is
    # the whole conversion.
    return Matrix(rows).transposed()


def _current(chunk, kind) -> list:
    """The chunk's current translate/rotate/scale, read off its params.

    Read rather than remembered: a chunk's params move for reasons that are not this drag
    -- a slider, an expression, an upstream cook -- and a handle that cached them would
    write back a stale two thirds of the matrix on its next tick.
    """
    names = {_TRANSLATE: ("tx", "ty", "tz"),
             _ROTATE: ("rx", "ry", "rz"),
             _SCALE: ("sx", "sy", "sz")}[kind]
    out = []
    for name in names:
        param = chunk.get_param(name)
        value = param.value if param is not None else None
        if value is None:
            value = 1.0 if kind == _SCALE else 0.0
        out.append(float(value))
    return out


def _compose(translate, rotate, scale) -> Gf.Matrix4d:
    """One matrix from the three components, in the order a chunk decomposes it back.

    Built here rather than read off the prim: the prim's transform is what the chunk
    already produced, so feeding it back would fold the chunk's own op in twice.
    """
    m = Gf.Matrix4d(1.0)
    m.SetScale(Gf.Vec3d(*scale))
    rot = (Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(1, 0, 0), rotate[0]))
           * Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 1, 0), rotate[1]))
           * Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), rotate[2])))
    return m * rot * Gf.Matrix4d().SetTranslate(Gf.Vec3d(*translate))


def write_component(desc, chunk, kind, axis: int, value: float) -> None:
    """Write one axis of one component back through the chunk. The whole write path.

    A `transform` desc takes a matrix, so the two components this drag is not touching are
    read from the chunk and composed back in unchanged -- which is also what keeps a
    rotate handle from resetting a translate. A desc of the component's own type takes the
    vector directly.

    Every tick, never batched to the end of the drag (contract §5): the chunk cooks per
    write, and that is what makes the prim follow the cursor.
    """
    components = {_TRANSLATE: _current(chunk, _TRANSLATE),
                  _ROTATE: _current(chunk, _ROTATE),
                  _SCALE: _current(chunk, _SCALE)}
    components[kind][axis] = value
    if desc.type == "transform":
        payload = _compose(components[_TRANSLATE], components[_ROTATE], components[_SCALE])
    elif desc.type == kind:
        payload = Gf.Vec3d(*components[kind])
    else:
        return
    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, payload)


class _ChunkHandle(bpy.types.Gizmo):
    """One axis of one component, drawn as a Blender preset shape.

    `invoke`/`exit` are the reason this is a subclass at all: they bracket the drag with
    the chunk's own GIZMO_START/GIZMO_STOP, which is where a host's undo block goes and
    what tells a chunk a gesture is running.
    """

    bl_idname = "USDCHUNKS_GT_handle"

    __slots__ = ("chunk", "desc", "kind", "axis", "_start_value", "_start_mouse")

    def setup(self):
        self.chunk = None
        self.desc = None
        self.kind = _TRANSLATE
        self.axis = 0
        self._start_value = 0.0

    def draw(self, context):
        self._draw(context, select_id=None)

    def draw_select(self, context, select_id):
        self._draw(context, select_id=select_id)

    def _draw(self, context, select_id=None):
        shape = {"translate": "ARROW", "scale": "BOX", "rotate": "CIRCLE"}[self.kind]
        if shape == "CIRCLE":
            self.draw_preset_circle(self.matrix_basis, axis="POS_Z", select_id=select_id)
        else:
            self.draw_preset_arrow(self.matrix_basis, axis="POS_Z", select_id=select_id)

    def invoke(self, context, event):
        if self.chunk is not None:
            self.chunk.start_gizmo()
        self._start_value = self._read()
        return {"RUNNING_MODAL"}

    def exit(self, context, cancel):
        if cancel:
            self._write(self._start_value)
        if self.chunk is not None:
            self.chunk.stop_gizmo()

    def modal(self, context, event, tweak):
        # target_get_value/target_set_value carry the drag; this only has to report that it
        # is still running so Blender keeps sending events.
        return {"RUNNING_MODAL"}

    def _read(self) -> float:
        if self.chunk is None:
            return 0.0
        value = _current(self.chunk, self.kind)[self.axis]
        return value / _DEG if self.kind == _ROTATE else value

    def _write(self, value: float) -> None:
        if self.chunk is None or self.desc is None:
            return
        # Blender hands a dial its angle in radians; the contract takes degrees.
        write_component(self.desc, self.chunk, self.kind, self.axis,
                        value * _DEG if self.kind == _ROTATE else value)


class USDCHUNKS_GGT_chunk_gizmos(bpy.types.GizmoGroup):
    """The handles for whatever prim is picked in the live viewport."""

    bl_idname = "USDCHUNKS_GGT_chunk_gizmos"
    bl_label = "USD Chunk Handles"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_options = {"3D", "PERSISTENT", "SHOW_MODAL_ALL"}

    @classmethod
    def poll(cls, context):
        # Only with the live view up: without it there is nothing on screen for a handle to
        # sit on, and the picked path is whatever was picked before it was turned off.
        if not viewport.hydra_view_enabled():
            return False
        stage = viewport.active_stage()
        prim = _picked_prim(stage)
        if prim is None:
            return False
        return bool(_visible_descs(prim, _active_view()))

    def setup(self, context):
        self._handles = []
        self._rebuild(context)

    def refresh(self, context):
        self._rebuild(context)

    def _rebuild(self, context):
        stage = viewport.active_stage()
        prim = _picked_prim(stage)
        view = _active_view()
        descs = _visible_descs(prim, view) if prim is not None else []
        self.gizmos.clear()
        self._handles = []
        for desc in descs:
            chunk = _chunk_for(view, desc)
            if chunk is None:
                continue
            kinds = ((_TRANSLATE, _ROTATE, _SCALE) if desc.type == "transform"
                     else (desc.type,) if desc.type in (_TRANSLATE, _ROTATE, _SCALE) else ())
            base = _anchor_matrix(stage, desc)
            for kind in kinds:
                for axis in range(3):
                    self._add(desc, chunk, kind, axis, base)

    def _add(self, desc, chunk, kind, axis, base) -> None:
        gz = self.gizmos.new(_ChunkHandle.bl_idname)
        gz.chunk = chunk
        gz.desc = desc
        gz.kind = kind
        gz.axis = axis
        gz.color = _AXIS_COLOR[axis]
        gz.alpha = 0.6
        gz.color_highlight = _AXIS_COLOR[axis]
        gz.alpha_highlight = 1.0
        gz.use_draw_modal = True
        # Each handle points down its own axis: the presets draw along +Z, so the basis
        # carries the rotation that aims them.
        aim = {0: Matrix.Rotation(1.5707963, 4, "Y"),
               1: Matrix.Rotation(-1.5707963, 4, "X"),
               2: Matrix()}[axis]
        gz.matrix_basis = base @ aim
        gz.target_set_handler("offset", get=gz._read, set=gz._write)
        self._handles.append(gz)

    def draw_prepare(self, context):
        """Follow the prim while something else moves it -- another handle, a slider, an
        upstream cook. The anchor matrix is re-read rather than cached at setup, which is
        the same reason the contract gives for re-reading `offset` on stage change."""
        stage = viewport.active_stage()
        for gz in self._handles:
            if gz.desc is None:
                continue
            aim = {0: Matrix.Rotation(1.5707963, 4, "Y"),
                   1: Matrix.Rotation(-1.5707963, 4, "X"),
                   2: Matrix()}[gz.axis]
            gz.matrix_basis = _anchor_matrix(stage, gz.desc) @ aim


def _chunk_for(view, desc):
    """The live chunk object a desc names, or None.

    `apply_bindings` takes the prim path straight off the record, so this is only needed
    for the start/stop bracket, which is a method on the chunk.
    """
    if view is None:
        return None
    return view.chunks.get(str(desc.chunk_prim))


_CLASSES = (_ChunkHandle, USDCHUNKS_GGT_chunk_gizmos)


def _registered(cls) -> bool:
    """Whether Blender already holds this gizmo class.

    Gizmo and GizmoGroup types are NOT published on `bpy.types` under their class name the
    way an operator or a panel is -- they live in their own type registry, reachable only
    through the base class. A guard that asked `bpy.types` therefore said "not registered"
    both times: register ran twice (a ValueError on a re-enable) and unregister never ran
    at all, leaving a stale class pointing at a reloaded module.
    """
    base = bpy.types.GizmoGroup if issubclass(cls, bpy.types.GizmoGroup) else bpy.types.Gizmo
    return base.bl_rna_get_subclass_py(cls.bl_idname) is not None


def register() -> None:
    for cls in _CLASSES:
        if not _registered(cls):
            bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_CLASSES):
        if _registered(cls):
            bpy.utils.unregister_class(cls)
