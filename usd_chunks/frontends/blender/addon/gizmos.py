"""Transforming a chunk-driven prim from Blender's viewport.

The host half of `docs/gizmo-host-contract.md`. usd_chunks records `ui:gizmo:*` on a chunk
prim and owns the write-back; this file is Blender's interpretation: which handles exist
for what the user picked (`chunk_gizmo.resolve_gizmos`), and the per-tick
`chunk_gizmo.apply_bindings` that moves the prim by moving the chunk's params.

**G, R, S -- not a drawn widget.** The first version of this drew a TRS gizmo, three arrows
and three dials, the way usdview does. That was the wrong shape for this host: a Blender
user's hands already know `G`, `G X`, `R Z`, `S`, and a widget that has to be found and
dragged is slower than the keys for anyone who has used Blender. So the selection is a
pivot dot, exactly as Blender marks a selected object's origin, and the gestures are
Blender's own -- pressed on the keyboard, constrained with `X`/`Y`/`Z`, confirmed with a
click, cancelled with Escape.

The keymap items sit in the addon keyconfig, which Blender checks before its own, and their
poll only passes when a chunk-driven prim is picked in the live viewport. With nothing
picked, `G` is Blender's grab as always.

**Picked, not selected.** A prim here comes from viewport picking (viewport.py), since a
Hydra-drawn prim is not a Blender object and cannot be in `context.selected_objects`.
"""
from __future__ import annotations

import logging
import math

import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector
from pxr import Gf, Usd, UsdGeom

from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.frontends.blender import addon
from usd_chunks.frontends.blender import nodegraph as bg
from usd_chunks.frontends.blender.addon import viewport

logger = logging.getLogger(__name__)

#: Which component each gesture drives, and the order the contract gives for a `transform`
#: driver: translate, rotate, scale.
_TRANSLATE, _ROTATE, _SCALE = "translate", "rotate", "scale"

#: What the pivot dot looks like: Blender's own selected-origin orange, a few pixels wide.
_DOT_COLOR = (1.0, 0.65, 0.15, 1.0)
_DOT_OUTLINE = (0.0, 0.0, 0.0, 0.8)
_DOT_SIZE = 9.0

#: Seconds between the redraws a gesture asks for: one a frame at 60 Hz.
_FRAME = 1.0 / 60.0

_draw_state = {"handle": None}

#: Degrees per radian, spelled once. A `rotate` value goes back as Euler XYZ in DEGREES
#: (contract §3) while Blender works in radians, and this is the one place that converts.
_DEG = 57.29577951308232


def _active_view():
    """The add-on's live graph view, or None.

    The add-on package imports this module in order to register it, so `addon` is still
    half-loaded when this module is; that is fine, because it is only read here, at call
    time, by which point registration has finished.
    """
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


def placement_desc(prim, view):
    """The one handle a G/R/S gesture drives, or None.

    A `transform` handle first, since it takes the whole matrix; otherwise the handle of
    the gesture's own kind, which is how a chunk offering only some of the three
    (rbd_solver declares translate and rotate offsets and no scale) is still driveable.
    """
    descs = _visible_descs(prim, view)
    for desc in descs:
        if desc.type == "transform":
            return desc
    for desc in descs:
        if desc.type in (_TRANSLATE, _ROTATE, _SCALE):
            return desc
    return None


def _chunk_for(view, desc):
    """The live chunk object a desc names, or None. Needed for the start/stop bracket,
    which is a method on the chunk; the write itself goes by prim path.

    Falls back to the scene's own lookup: a chunk made a moment ago by
    `ensure_transform_chunk` is not in the view's table until it next reloads, so G on a
    prim nothing drove made the chunk and then cancelled with nothing to move.
    """
    if desc is None:
        return None
    found = view.chunks.get(str(desc.chunk_prim)) if view is not None else None
    return found or chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)


def _centre_world(prim, matrix: Gf.Matrix4d) -> Gf.Vec3d:
    """The middle of what the prim draws, in world space, or its origin if it draws nothing.

    Not the transform origin: Kitchen_set's props carry a transform on the group and its
    exact inverse on the mesh, so a cup on a table has its origin at the world origin. A
    pivot out there put the dot nowhere near the cup, and a grab -- which reads the mouse at
    the pivot's depth -- threw it across the room. usdview's handle sits on the bbox centroid
    for the same reason (usd_gizmos handler.updateGizmoPosition).
    """
    cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_, UsdGeom.Tokens.proxy,
                                  UsdGeom.Tokens.render], useExtentsHint=True)
    bound = cache.ComputeWorldBound(prim)
    if bound.GetRange().IsEmpty():
        return matrix.Transform(Gf.Vec3d(0, 0, 0))
    return bound.ComputeCentroid()


def anchor_world(stage, desc):
    """Where the pivot is, in world space: the chunk's pivot when it has an opinion about
    one, else the middle of the anchor's geometry.

    `offset` is a point in the anchor's OWN object space (contract §2), so it maps through
    the anchor's local-to-world rather than being added in world space.
    """
    if stage is None or desc is None:
        return None
    anchors = [stage.GetPrimAtPath(p) for p in desc.anchor]
    anchors = [a for a in anchors if a and a.IsValid()]
    if not anchors:
        return None
    xf = UsdGeom.Xformable(anchors[0])
    if not xf:
        return None
    matrix = xf.ComputeLocalToWorldTransform(0)
    if desc.offset is not None:
        world = matrix.Transform(Gf.Vec3d(*desc.offset))
    else:
        world = _centre_world(anchors[0], matrix)
    return Vector((world[0], world[1], world[2]))


def _anchor_matrices(stage, desc):
    """The anchor's composed local transform and its parent's local-to-world, or None.

    A `transform` handle hands the chunk the prim's whole LOCAL matrix -- the chunk divides
    its upstream back out itself (transform.gizmo_changed) -- so a gesture is built on these
    two rather than on the chunk's params.
    """
    if stage is None or desc is None or not desc.anchor:
        return None
    prim = stage.GetPrimAtPath(desc.anchor[0])
    xf = UsdGeom.Xformable(prim) if prim and prim.IsValid() else None
    if not xf:
        return None
    local = xf.GetLocalTransformation(Usd.TimeCode.Default())
    if isinstance(local, tuple):
        local = local[0]
    parent = prim.GetParent()
    parent_xf = UsdGeom.Xformable(parent) if parent and not parent.IsPseudoRoot() else None
    parent_world = parent_xf.ComputeLocalToWorldTransform(0) if parent_xf else Gf.Matrix4d(1.0)
    return Gf.Matrix4d(local), Gf.Matrix4d(parent_world)


def parent_rotation(stage, desc) -> Matrix:
    """The rotation taking a world-space direction into the space the chunk's params are
    in: the inverse of the anchor's PARENT's local-to-world.

    A chunk's translate is the prim's own local translation, so a world-space drag on a
    parented prim has to come back through the parent before it means anything. Rotation
    and scale only -- a translation offset is not a direction.
    """
    if stage is None or desc is None or not desc.anchor:
        return Matrix()
    prim = stage.GetPrimAtPath(desc.anchor[0])
    if not (prim and prim.IsValid()):
        return Matrix()
    parent = prim.GetParent()
    if not (parent and parent.IsValid()) or parent.IsPseudoRoot():
        return Matrix()
    xf = UsdGeom.Xformable(parent)
    if not xf:
        return Matrix()
    gf = xf.ComputeLocalToWorldTransform(0).GetInverse()
    rows = [[gf[r][c] for c in range(4)] for r in range(4)]
    return Matrix(rows).transposed().to_3x3().to_4x4()


# ---------------------------------------------------------------------------
# the maths of a gesture -- pure, so it can be tested without a viewport
# ---------------------------------------------------------------------------

def constrain(delta, axis, basis: Matrix) -> Vector:
    """A drag delta reduced to its constrained part.

    `X`/`Y`/`Z` during a gesture is Blender's axis lock, and the axis is the one in
    `basis` -- the world's for a world-space handle, the anchor's own for a local one --
    so the projection is onto that column, not onto a global axis. Unconstrained, the
    delta is whatever the mouse did.
    """
    if axis is None:
        return Vector(delta)
    index = {"X": 0, "Y": 1, "Z": 2}[axis]
    direction = Vector(basis.col[index][:3])
    if direction.length == 0.0:
        return Vector(delta)
    direction.normalize()
    return direction * Vector(delta).dot(direction)


def screen_angle(centre_2d, start_2d, now_2d) -> float:
    """The angle the mouse has swept about the pivot, in radians.

    Blender's own rotate: the pivot is the anchor on screen and the angle is between where
    the drag began and where it is now. Zero when either arm has no length, since an angle
    about a point the mouse sits exactly on is not defined.
    """
    start = Vector(start_2d) - Vector(centre_2d)
    now = Vector(now_2d) - Vector(centre_2d)
    if start.length == 0.0 or now.length == 0.0:
        return 0.0
    return math.atan2(now.y, now.x) - math.atan2(start.y, start.x)


def screen_scale(centre_2d, start_2d, now_2d) -> float:
    """How much further from the pivot the mouse is than when the drag began.

    Blender's own scale. One when the gesture started on the pivot, since a ratio against
    no distance is not a number -- and "no change" is the honest answer for a gesture that
    cannot say anything yet.
    """
    start = (Vector(start_2d) - Vector(centre_2d)).length
    now = (Vector(now_2d) - Vector(centre_2d)).length
    if start == 0.0:
        return 1.0
    return now / start


def rotated(values, axis, radians: float, view_axis=None) -> list:
    """Euler XYZ degrees after turning by `radians`.

    A constrained turn adds to that axis's own channel, which is exact and leaves the
    other two as the user had them. An unconstrained one is about the view axis, so it
    goes through a matrix and back -- which is what `chunk_gizmo.decompose` is for, and
    doing it there keeps one Euler convention in the codebase instead of two.
    """
    degrees = radians * _DEG
    if axis is not None:
        index = {"X": 0, "Y": 1, "Z": 2}[axis]
        out = list(values)
        out[index] += degrees
        return out
    direction = Vector(view_axis or (0.0, 0.0, 1.0))
    if direction.length == 0.0:
        return list(values)
    direction.normalize()
    current = (Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(1, 0, 0), values[0]))
               * Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 1, 0), values[1]))
               * Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), values[2])))
    turn = Gf.Matrix4d().SetRotate(
        Gf.Rotation(Gf.Vec3d(direction.x, direction.y, direction.z), degrees))
    euler = chunk_gizmo.decompose(current * turn)["rotate"]
    return [euler[0], euler[1], euler[2]]


def scaled(values, axis, factor: float) -> list:
    """Scale after a gesture: one axis when constrained, all three when not."""
    if axis is None:
        return [v * factor for v in values]
    index = {"X": 0, "Y": 1, "Z": 2}[axis]
    out = list(values)
    out[index] *= factor
    return out


def about(centre, linear: Gf.Matrix4d) -> Gf.Matrix4d:
    """A world-space turn or scale that holds `centre` still."""
    c = Gf.Vec3d(centre[0], centre[1], centre[2])
    return Gf.Matrix4d().SetTranslate(-c) * linear * Gf.Matrix4d().SetTranslate(c)


def turn(axis, radians: float) -> Gf.Matrix4d:
    """A rotation about a world direction."""
    return Gf.Matrix4d().SetRotate(
        Gf.Rotation(Gf.Vec3d(axis[0], axis[1], axis[2]), radians * _DEG))


def stretch(factor: float, axis=None) -> Gf.Matrix4d:
    """Scale by `factor` along one world direction, or uniformly when there is none."""
    if axis is None:
        return Gf.Matrix4d().SetScale(Gf.Vec3d(factor, factor, factor))
    a = Gf.Vec3d(axis[0], axis[1], axis[2]).GetNormalized()
    # I + (f-1) a^T a: the part along `a` scaled, the rest untouched. Row-vector, like Gf.
    rows = [[(1.0 if r == c else 0.0) + (factor - 1.0) * a[r] * a[c] for c in range(3)] + [0.0]
            for r in range(3)]
    return Gf.Matrix4d(*[v for row in rows for v in row], 0.0, 0.0, 0.0, 1.0)


def gesture_local(start_local: Gf.Matrix4d, parent_world: Gf.Matrix4d,
                  world_delta: Gf.Matrix4d) -> Gf.Matrix4d:
    """The anchor's new local matrix after `world_delta` moves it in world space.

    The prim keeps its place relative to the pivot, the way usdview's handle carries a
    selection with an offset -- so a turn about the middle of a cup turns the cup there
    rather than swinging it round its transform origin, wherever that happens to be.
    """
    return start_local * parent_world * world_delta * parent_world.GetInverse()


# ---------------------------------------------------------------------------
# reading and writing the chunk
# ---------------------------------------------------------------------------

def _current(chunk, kind) -> list:
    """The chunk's translate/rotate/scale, read off its params rather than remembered: a
    param moves for reasons that are not this gesture, and a cached one would write back a
    stale two thirds of the matrix.
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

    Built from the params rather than read off the prim: the prim's transform is what the
    chunk already produced, so feeding it back would fold the chunk's own op in twice.
    """
    m = Gf.Matrix4d(1.0)
    m.SetScale(Gf.Vec3d(*scale))
    rot = (Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(1, 0, 0), rotate[0]))
           * Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 1, 0), rotate[1]))
           * Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), rotate[2])))
    return m * rot * Gf.Matrix4d().SetTranslate(Gf.Vec3d(*translate))


def write_components(desc, chunk, components) -> None:
    """Send the components back through the chunk. The whole write path.

    A `transform` desc takes a matrix, so all three go together and the chunk decomposes
    once; a desc of one component's own type takes that vector. Every tick, never batched
    to the end of the gesture (contract §5): apply_bindings cooks at most every
    chunk_gizmo.DRAG_INTERVAL with the newest value, and that is what makes the prim follow
    the cursor.
    """
    if desc.type == "transform":
        payload = _compose(components[_TRANSLATE], components[_ROTATE], components[_SCALE])
    elif desc.type in components:
        payload = Gf.Vec3d(*components[desc.type])
    else:
        return
    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, payload)


# ---------------------------------------------------------------------------
# the pivot dot
# ---------------------------------------------------------------------------

def pivot_point(stage, view):
    """Where to draw the dot, or None: the driven anchor if a chunk owns this prim, else
    the middle of the prim -- a picked-but-undriven prim still gets a dot, since a gesture
    on it creates the chunk that drives it and the dot is the only sign the click landed.
    """
    prim = _picked_prim(stage)
    if prim is None:
        return None
    desc = placement_desc(prim, view)
    if desc is not None:
        point = anchor_world(stage, desc)
        if point is not None:
            return point
    xf = UsdGeom.Xformable(prim)
    if not xf:
        return None
    world = _centre_world(prim, xf.ComputeLocalToWorldTransform(0))
    return Vector((world[0], world[1], world[2]))


def _disc(centre, radius: float, segments: int = 24) -> list:
    """A filled circle's TRI_FAN vertices in region pixels: the centre, then the rim."""
    rim = [(centre[0] + radius * math.cos(2.0 * math.pi * i / segments),
            centre[1] + radius * math.sin(2.0 * math.pi * i / segments))
           for i in range(segments + 1)]
    return [(centre[0], centre[1])] + rim


def _draw_pivot():
    if not viewport.hydra_view_enabled():
        return
    # Mid-gesture the dot is where the gesture puts the pivot, not where the last cook
    # did: the chunk cooks at most every chunk_gizmo.DRAG_INTERVAL, and the dot must stay
    # on the cursor every frame in between.
    point = _draw_state.get("drag_pivot")  # already in Blender's space
    if point is None:
        stage = viewport.active_stage()
        point = pivot_point(stage, _active_view())
        if point is not None:
            point = viewport.to_blender(stage, point)
    if point is None:
        return
    try:
        # A triangle-fan disc in screen space, not a GL point: point shaders draw square
        # on some backends whatever their name says, and a disc is round everywhere.
        centre = view3d_utils.location_3d_to_region_2d(
            bpy.context.region, bpy.context.region_data, point)
        if centre is None:  # behind the view
            return
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        gpu.state.blend_set("ALPHA")
        shader.bind()
        # A dark rim under the orange, as Blender outlines a selected object's origin.
        for size, color in ((_DOT_SIZE + 2.0, _DOT_OUTLINE), (_DOT_SIZE, _DOT_COLOR)):
            batch = batch_for_shader(shader, "TRI_FAN", {"pos": _disc(centre, size / 2.0)})
            shader.uniform_float("color", color)
            batch.draw(shader)
        gpu.state.blend_set("NONE")
    except Exception:
        logger.debug("pivot dot draw failed", exc_info=True)


def _redraw() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# ---------------------------------------------------------------------------
# the gesture
# ---------------------------------------------------------------------------

class USDCHUNKS_OT_transform(bpy.types.Operator):
    """Move, rotate or scale the picked prim by moving the chunk that drives it.

    Blender's own grab/rotate/scale gestures against a USD prim: the mouse drives, `X`,
    `Y` and `Z` constrain, a click or Return confirms, Escape or right-click puts it back.
    """

    bl_idname = "usd_chunks.transform"
    bl_label = "Transform Chunk Prim"
    bl_options = {"REGISTER", "UNDO"}

    mode: bpy.props.EnumProperty(
        items=[(_TRANSLATE, "Translate", ""), (_ROTATE, "Rotate", ""), (_SCALE, "Scale", "")],
        default=_TRANSLATE,
    )

    @classmethod
    def poll(cls, context):
        # Only with the live view up and a prim picked. Anything else and this keymap item
        # stands aside, so G is Blender's own grab as always.
        if getattr(context.space_data, "type", "") != "VIEW_3D":
            return False
        if not viewport.hydra_view_enabled():
            return False
        return _picked_prim(viewport.active_stage()) is not None

    def invoke(self, context, event):
        self._region = context.region
        self._rv3d = context.region_data
        stage = viewport.active_stage()
        prim = _picked_prim(stage)
        view = _active_view()
        desc = placement_desc(prim, view) if prim is not None else None
        if desc is None and prim is not None:
            # Contract section 8: a prim nothing drives gets the chunk that will, rather
            # than a direct write the next cook would erase.
            desc = chunk_gizmo.ensure_transform_chunk(prim)
            view = _active_view()
        self._chunk = _chunk_for(view, desc)
        if desc is None or self._chunk is None:
            self.report({"WARNING"}, "nothing here for a chunk to move")
            return {"CANCELLED"}

        self._desc = desc
        self._stage = stage
        self._axis = None
        self._start = {kind: _current(self._chunk, kind)
                       for kind in (_TRANSLATE, _ROTATE, _SCALE)}
        # The gesture runs in Blender's space -- the mouse, the pivot, X/Y/Z -- and crosses
        # into the stage's (viewport.stage_to_blender) only where it writes.
        self._to_blender = viewport.stage_to_blender(stage)
        pivot = anchor_world(stage, desc)
        self._pivot = (self._to_blender @ pivot) if pivot is not None else Vector((0.0, 0.0, 0.0))
        self._pivot_2d = view3d_utils.location_3d_to_region_2d(
            self._region, self._rv3d, self._pivot) or Vector((0.0, 0.0))
        self._start_mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        self._start_world = self._world_at(self._start_mouse)
        c3 = self._to_blender.to_3x3()
        parent = parent_rotation(stage, desc).to_3x3()
        self._to_local = (parent @ c3.inverted_safe()).to_4x4()
        # The basis a constraint is taken in: the world's for a world-space handle, the
        # anchor's own for a local one (contract section 3 -- absent space means local).
        # X/Y/Z name Blender's axes either way, turned by the parent's rotation for a local
        # handle: the parent's rotation is carried into Blender's space (c3 P^-1 c3^-1)
        # rather than its axes, so on a Y-up stage Z is still up -- R Z spins a prim
        # upright instead of tipping it over about the stage's Z, which is Blender's -Y.
        self._basis = (Matrix() if getattr(desc, "space", "local") == "world"
                       else (c3 @ parent.inverted_safe() @ c3.inverted_safe()).to_4x4())
        # A transform handle moves the whole local matrix about the pivot; None for a
        # component handle, which still writes its own vector.
        self._matrices = (_anchor_matrices(stage, desc) if desc.type == "transform"
                          else None)
        _draw_state["drag_pivot"] = self._pivot.copy()
        self._chunk.start_gizmo()
        # A redraw every frame of the gesture: the chunk cooks a held-back write on a timer
        # (chunk_gizmo.DRAG_INTERVAL), and Blender redraws for no stage change by itself.
        self._timer = context.window_manager.event_timer_add(_FRAME, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"},
                    f"{self.mode}: X/Y/Z to constrain, click to confirm, Esc to cancel")
        return {"RUNNING_MODAL"}

    def _world_at(self, mouse):
        return view3d_utils.region_2d_to_location_3d(
            self._region, self._rv3d, mouse, self._pivot)

    def modal(self, context, event):
        if event.type in {"X", "Y", "Z"} and event.value == "PRESS":
            # Pressing the same axis again releases it, the way Blender's second press
            # moves on -- here, simply back to unconstrained.
            self._axis = None if self._axis == event.type else event.type
            self._apply(event)
            return {"RUNNING_MODAL"}
        if event.type == "MOUSEMOVE":
            self._apply(event)
            return {"RUNNING_MODAL"}
        if event.type == "TIMER":
            _redraw()
            return {"RUNNING_MODAL"}
        if event.type in {"LEFTMOUSE", "RET", "NUMPAD_ENTER"} and event.value == "PRESS":
            self._end(context)
            return {"FINISHED"}
        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            if self._matrices is not None:
                chunk_gizmo.apply_bindings(self._desc.chunk_prim, self._desc.name,
                                           self._matrices[0])
            else:
                write_components(self._desc, self._chunk, self._start)
            self._end(context)
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def _end(self, context) -> None:
        """Close the gesture: the dot back on the prim, the last write cooked, a redraw."""
        context.window_manager.event_timer_remove(self._timer)
        _draw_state["drag_pivot"] = None
        self._chunk.stop_gizmo()
        _redraw()

    def _apply(self, event):
        mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        if self._matrices is not None:
            self._apply_matrix(mouse)
            _redraw()
            return
        components ={kind: list(values) for kind, values in self._start.items()}
        if self.mode == _TRANSLATE:
            delta = constrain(self._world_at(mouse) - self._start_world,
                              self._axis, self._basis)
            _draw_state["drag_pivot"] = self._pivot + delta
            local = self._to_local @ delta
            components[_TRANSLATE] = [self._start[_TRANSLATE][i] + local[i] for i in range(3)]
        elif self.mode == _ROTATE:
            angle = screen_angle(self._pivot_2d, self._start_mouse, mouse)
            view_axis = self._to_blender.inverted_safe().to_3x3() @ -Vector(
                self._rv3d.view_matrix.col[2][:3])
            components[_ROTATE] = rotated(self._start[_ROTATE], self._axis, angle, view_axis)
        else:
            factor = screen_scale(self._pivot_2d, self._start_mouse, mouse)
            components[_SCALE] = scaled(self._start[_SCALE], self._axis, factor)
        write_components(self._desc, self._chunk, components)
        _redraw()

    def _apply_matrix(self, mouse):
        """A transform handle's tick: the gesture as a world-space change about the pivot,
        folded into the anchor's local matrix and handed to the chunk whole.

        The chunk's own TRS alone would be wrong for any prim with a transform beneath the
        chunk's op -- the chunk divides that upstream back out of what it is given, so it
        has to be in there.
        """
        view_axis = -Vector(self._rv3d.view_matrix.col[2][:3])
        axis = (Vector(self._basis.col[{"X": 0, "Y": 1, "Z": 2}[self._axis]][:3])
                if self._axis is not None else None)
        if self.mode == _TRANSLATE:
            delta = constrain(self._world_at(mouse) - self._start_world,
                              self._axis, self._basis)
            world_delta = Gf.Matrix4d().SetTranslate(Gf.Vec3d(*delta))
            _draw_state["drag_pivot"] = self._pivot + delta
        elif self.mode == _ROTATE:
            angle = screen_angle(self._pivot_2d, self._start_mouse, mouse)
            if axis is None:
                axis = view_axis
            elif axis.dot(view_axis) < 0.0:
                # Turn the way the mouse sweeps on screen, whichever way the axis faces.
                axis = -axis
            world_delta = about(self._pivot, turn(axis, angle))
        else:
            factor = screen_scale(self._pivot_2d, self._start_mouse, mouse)
            world_delta = about(self._pivot, stretch(factor, axis))
        # The change was built in Blender's space; the same change in the stage's is
        # C * D * C^-1 in Gf's row-vector order, C being stage_to_blender.
        c = Gf.Matrix4d(*[v for row in self._to_blender.transposed() for v in row])
        world_delta = c * world_delta * c.GetInverse()
        local, parent_world = self._matrices
        chunk_gizmo.apply_bindings(self._desc.chunk_prim, self._desc.name,
                                   gesture_local(local, parent_world, world_delta))


_CLASSES = (USDCHUNKS_OT_transform,)

#: Blender's own transform keys, for a USD prim.
_KEYS = (
    (USDCHUNKS_OT_transform, "G", {"mode": _TRANSLATE}),
    (USDCHUNKS_OT_transform, "R", {"mode": _ROTATE}),
    (USDCHUNKS_OT_transform, "S", {"mode": _SCALE}),
)

#: Where the keys are bound: (keymap name, space type). "Object Mode", not "3D View".
#: Blender checks the Object Mode keymap first, and its own G/R/S there took every press,
#: so a G item in "3D View" never ran.
_KEYMAP = ("Object Mode", "EMPTY")

_keymaps: list = []


def _register_keymap() -> None:
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:  # background/headless Blender has no addon keyconfig
        return
    km = kc.keymaps.new(name=_KEYMAP[0], space_type=_KEYMAP[1])
    for operator, key, props in _KEYS:
        kmi = km.keymap_items.new(operator.bl_idname, key, "PRESS")
        for name, value in props.items():
            setattr(kmi.properties, name, value)
        _keymaps.append((km, kmi))


def _unregister_keymap() -> None:
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()


def register() -> None:
    for cls in _CLASSES:
        # cls.is_registered, not a bpy.types lookup: Blender addresses an operator by its
        # bl_idname and does NOT publish it on bpy.types under its class name, so asking
        # bpy.types answers "no" both times -- register would run twice (a ValueError on a
        # re-enable) and unregister would never run, leaving a stale class behind a
        # reloaded module. The same trap as the gizmo type registry.
        if not cls.is_registered:
            bpy.utils.register_class(cls)
    if _draw_state["handle"] is None:
        _draw_state["handle"] = bpy.types.SpaceView3D.draw_handler_add(
            _draw_pivot, (), "WINDOW", "POST_PIXEL")
    _register_keymap()


def unregister() -> None:
    _unregister_keymap()
    if _draw_state["handle"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_state["handle"], "WINDOW")
        _draw_state["handle"] = None
    for cls in reversed(_CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
