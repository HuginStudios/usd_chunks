"""Painting through a chunk's brush gizmo from Blender's viewport.

The host half of a `brush` handle in `docs/gizmo-host-contract.md`. The chunk decides what a
stroke means (falloff, strength, add or subtract). This file only finds where the cursor meets
the surface and sends those points to `chunk_gizmo.apply_stroke`.

**How you use it.** Select a brush chunk (Scatter Brush) in the graph, or Ctrl+Click a prim
it paints, then press `B` in the viewport. A ring follows the surface under the cursor.
Left-drag paints, `F` resizes the ring, Escape or right-click stops. You can still orbit,
pan and zoom while the brush is on.

**The hit test is ours, not Hydra's.** Viewport picking only says which prim is under the
cursor, and on some builds only by bounding box. A brush needs the point on the surface and
its normal, so the meshes the chunk paints go into a `BVHTree`, built once per stroke.
"""
from __future__ import annotations

import logging
import math

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from pxr import Gf, Usd, UsdGeom

from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.frontends.blender import addon
from usd_chunks.frontends.blender import nodegraph as bg
from usd_chunks.frontends.blender.addon import viewport

logger = logging.getLogger(__name__)

#: Ring colours. Blue, as in usdview, so the ring is not mistaken for a selection.
_HOVER_COLOR = (0.30, 0.65, 1.00, 1.0)
_PAINTING_COLOR = (0.55, 0.85, 1.00, 1.0)

#: Segments in the ring.
_SEGMENTS = 48

#: How far the ring sits above the surface, as a fraction of its radius, so it does not
#: z-fight with the mesh under it.
_LIFT = 0.01

#: The ring's size when the chunk has no radius: this fraction of the surface's diagonal.
_DEFAULT_RADIUS_FRACTION = 0.05

#: How fast `F` resizes: the radius doubles for every this many pixels moved right.
_RESIZE_PIXELS_PER_DOUBLING = 150.0

#: Keys that still reach Blender while the brush is on, so the view can be moved.
_NAVIGATION = {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN",
               "TRACKPADZOOM", "MOUSEROTATE", "NDOF_MOTION",
               "NUMPAD_0", "NUMPAD_1", "NUMPAD_2", "NUMPAD_3", "NUMPAD_4", "NUMPAD_5",
               "NUMPAD_6", "NUMPAD_7", "NUMPAD_8", "NUMPAD_9", "NUMPAD_PERIOD",
               "NUMPAD_PLUS", "NUMPAD_MINUS"}

#: What the draw handler draws: the ring's centre, normal and radius, and whether a stroke
#: is under way. Empty when no brush is on.
_ring: dict = {}
_draw_state = {"handle": None}


# ---------------------------------------------------------------------------
# which chunk paints
# ---------------------------------------------------------------------------

def _brush_descs(prim) -> list:
    return [d for d in chunk_gizmo.resolve_gizmos(prim) if d.type == "brush"]


def brush_desc(stage, view):
    """Find the brush handle the user means.

    A brush chunk selected in the graph comes first, since the chunk already names the
    surface it paints. Otherwise the picked prim and its ancestors are asked.

    Args:
        stage: The network's stage.
        view: The live graph view, or None.

    Returns:
        The brush's GizmoDesc, or None.
    """
    if stage is None:
        return None
    for path in sorted(getattr(view, "selected_nodes", ()) or ()):
        descs = _brush_descs(stage.GetPrimAtPath(path))
        if descs:
            return descs[0]
    for path in bg.get_picked_prim_paths():
        prim = stage.GetPrimAtPath(path)
        while prim and prim.IsValid() and not prim.IsPseudoRoot():
            descs = _brush_descs(prim)
            if descs:
                return descs[0]
            prim = prim.GetParent()
    return None


def _radius_param(chunk, desc):
    """The chunk param the ring is sized from, or None.

    A brush's `driver` names the params it reads; the one ending in `radius` is the size.
    """
    for entry in desc.driver:
        name = str(entry).split(".")[-1].split(":")[-1]
        if name == "radius":
            return chunk.get_param(name)
    return None


def brush_radius(chunk, desc, default: float) -> float:
    """The ring's radius in world units: the chunk's radius param, or `default`."""
    param = _radius_param(chunk, desc)
    value = getattr(param, "value", None) if param is not None else None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0.0 else default


def resized(radius: float, pixels: float) -> float:
    """The radius after an `F` drag of `pixels` to the right (negative is left)."""
    return radius * 2.0 ** (pixels / _RESIZE_PIXELS_PER_DOUBLING)


# ---------------------------------------------------------------------------
# where the cursor meets the surface
# ---------------------------------------------------------------------------

class Surface:
    """The meshes a brush paints, in world space, ready for ray tests.

    Walks every mesh under the anchors, as the Scatter Brush chunk does, so the two agree on
    what "the surface" is. World space at the default time, which is the space the chunk
    reads strokes in.

    Args:
        stage: The stage the meshes are on.
        paths: The anchor prim paths from the brush handle.
    """

    def __init__(self, stage, paths):
        self.tree = None
        self.diagonal = 0.0
        points, polygons = [], []
        for path in paths:
            prim = stage.GetPrimAtPath(path) if stage is not None else None
            if not (prim and prim.IsValid()):
                continue
            for p in Usd.PrimRange(prim):
                mesh = UsdGeom.Mesh(p)
                if not mesh:
                    continue
                world, faces = _world_mesh(mesh)
                if world is None:
                    continue
                offset = sum(len(block) for block in points)
                points.append(world)
                polygons.extend([index + offset for index in face] for face in faces)
        if not points:
            return
        world = np.concatenate(points)
        self.diagonal = float(np.linalg.norm(world.max(axis=0) - world.min(axis=0)))
        self.tree = BVHTree.FromPolygons(world.tolist(), polygons)

    def hit(self, origin, direction):
        """Cast a ray at the surface.

        Args:
            origin: The ray's start, in world space.
            direction: The ray's direction, in world space.

        Returns:
            (point, normal) as Vectors, with the normal turned to face the ray, or
            (None, None) on a miss.
        """
        if self.tree is None:
            return None, None
        direction = Vector(direction)
        point, normal, _index, _distance = self.tree.ray_cast(Vector(origin), direction)
        if point is None:
            return None, None
        normal = Vector(normal)
        if normal.dot(direction) > 0.0:
            normal = -normal
        return point, normal


def _world_mesh(mesh):
    """One mesh's points in world space and its faces as index lists, or (None, None)."""
    points = mesh.GetPointsAttr().Get()
    counts = mesh.GetFaceVertexCountsAttr().Get()
    indices = mesh.GetFaceVertexIndicesAttr().Get()
    if not points or not counts or not indices:
        return None, None
    matrix = np.asarray(
        UsdGeom.Xformable(mesh.GetPrim()).ComputeLocalToWorldTransform(Usd.TimeCode.Default()),
        dtype=np.float64,
    )
    # Row vectors, as in Gf: p * M, with the translation in the last row.
    world = np.asarray(points, dtype=np.float64) @ matrix[:3, :3] + matrix[3, :3]
    flat = np.asarray(indices, dtype=np.int64)
    if flat.size == 0 or flat.max() >= len(world) or flat.min() < 0:
        return None, None
    faces = [f.tolist() for f in np.split(flat, np.cumsum(np.asarray(counts))[:-1]) if len(f) >= 3]
    return world, faces


# ---------------------------------------------------------------------------
# the ring
# ---------------------------------------------------------------------------

def ring_points(centre, normal, radius: float) -> list:
    """The ring as a closed line strip, lying in the surface at `centre`.

    Args:
        centre: The hit point.
        normal: The surface normal at the hit.
        radius: The ring's radius in world units.

    Returns:
        `_SEGMENTS + 1` points; the last repeats the first to close the loop.
    """
    normal = Vector(normal)
    if normal.length < 1e-9:
        normal = Vector((0.0, 0.0, 1.0))
    normal.normalize()
    seed = Vector((1.0, 0.0, 0.0)) if abs(normal.x) < 0.9 else Vector((0.0, 1.0, 0.0))
    tangent = normal.cross(seed).normalized()
    bitangent = normal.cross(tangent)
    lifted = Vector(centre) + normal * radius * _LIFT
    out = []
    for i in range(_SEGMENTS + 1):
        theta = 2.0 * math.pi * i / _SEGMENTS
        out.append(tuple(lifted + (tangent * math.cos(theta) + bitangent * math.sin(theta))
                         * radius))
    return out


def _draw_ring():
    if not _ring or _ring.get("point") is None:
        return
    try:
        points = ring_points(_ring["point"], _ring["normal"], _ring["radius"])
        shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
        batch = batch_for_shader(shader, "LINE_STRIP", {"pos": points})
        gpu.state.blend_set("ALPHA")
        shader.bind()
        shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
        shader.uniform_float("lineWidth", 2.0)
        shader.uniform_float("color", _PAINTING_COLOR if _ring.get("painting") else _HOVER_COLOR)
        batch.draw(shader)
        gpu.state.blend_set("NONE")
    except Exception:
        logger.debug("brush ring draw failed", exc_info=True)


def _redraw() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# ---------------------------------------------------------------------------
# the gesture
# ---------------------------------------------------------------------------

class USDCHUNKS_OT_brush(bpy.types.Operator):
    """Paint with the selected brush chunk: left-drag paints, F resizes, Esc stops."""

    bl_idname = "usd_chunks.brush"
    bl_label = "Paint with Chunk Brush"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        # Only with the live view up and a brush chunk to paint through. Otherwise B stays
        # Blender's box select.
        if getattr(context.space_data, "type", "") != "VIEW_3D":
            return False
        if not viewport.hydra_view_enabled():
            return False
        return brush_desc(viewport.active_stage(), addon.get_view()) is not None

    def invoke(self, context, event):
        stage = viewport.active_stage()
        view = addon.get_view()
        desc = brush_desc(stage, view)
        chunk = view.chunks.get(str(desc.chunk_prim)) if desc is not None else None
        if chunk is None:
            self.report({"WARNING"}, "no brush chunk to paint with")
            return {"CANCELLED"}
        self._stage = stage
        self._desc = desc
        self._chunk = chunk
        self._region = context.region
        self._rv3d = context.region_data
        self._surface = Surface(stage, desc.anchor)
        if self._surface.tree is None:
            self.report({"WARNING"}, "the brush chunk has no mesh to paint on")
            return {"CANCELLED"}
        self._painting = False
        self._stroking = False
        self._resize_from = None
        _ring.clear()
        _ring.update(point=None, normal=None, radius=self._radius(), painting=False)
        self._hover(event)
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Brush: left-drag to paint, F to resize, Esc or right-click to stop")
        return {"RUNNING_MODAL"}

    def _radius(self) -> float:
        default = max(self._surface.diagonal * _DEFAULT_RADIUS_FRACTION, 1e-6)
        return brush_radius(self._chunk, self._desc, default)

    def _hover(self, event):
        """Move the ring to the surface under the mouse. Returns the hit, or (None, None)."""
        origin, direction = viewport.ray_from_region(
            self._region, self._rv3d, event.mouse_region_x, event.mouse_region_y)
        point, normal = self._surface.hit(origin, direction)
        if point is not None:
            # Off the mesh, the ring stays where it last was rather than vanishing.
            _ring["point"], _ring["normal"] = point, normal
        return point, normal

    def _paint(self, event, point, normal):
        """Send one sample to the chunk.

        The stroke opens on the first sample that hits, not on the press, so a click that
        misses the mesh does not make the chunk recook for nothing.
        """
        if point is None:
            return
        if not self._stroking:
            self._stroking = True
            self._chunk.start_gizmo()
        pressure = float(getattr(event, "pressure", 1.0) or 1.0)
        sample = (Gf.Vec3d(*point), pressure, Gf.Vec3d(*normal))
        chunk_gizmo.apply_stroke(self._chunk, self._desc.name, [sample])

    def _end_stroke(self):
        self._painting = False
        _ring["painting"] = False
        if self._stroking:
            self._stroking = False
            self._chunk.stop_gizmo()

    def _finish(self, context):
        self._end_stroke()
        _ring.clear()
        context.workspace.status_text_set(None)
        _redraw()

    def modal(self, context, event):
        if self._resize_from is not None:
            return self._modal_resize(event)

        if event.type == "MOUSEMOVE":
            point, normal = self._hover(event)
            _ring["radius"] = self._radius()
            if self._painting:
                self._paint(event, point, normal)
            _redraw()
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            # Rebuilt per stroke: an upstream chunk may have moved the mesh since the last.
            self._surface = Surface(self._stage, self._desc.anchor)
            self._painting = True
            _ring["painting"] = True
            self._paint(event, *self._hover(event))
            _redraw()
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            self._end_stroke()
            _redraw()
            return {"RUNNING_MODAL"}
        if event.type == "F" and event.value == "PRESS" and not self._painting:
            self._resize_from = (event.mouse_region_x, self._radius())
            return {"RUNNING_MODAL"}
        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            self._finish(context)
            return {"FINISHED"}
        if event.type in _NAVIGATION:
            return {"PASS_THROUGH"}
        return {"RUNNING_MODAL"}

    def _modal_resize(self, event):
        """F: move the mouse sideways to resize; click or Enter keeps it, Esc puts it back."""
        start_x, start_radius = self._resize_from
        if event.type == "MOUSEMOVE":
            _ring["radius"] = resized(start_radius, event.mouse_region_x - start_x)
            _redraw()
        elif event.type in {"LEFTMOUSE", "RET", "NUMPAD_ENTER"} and event.value == "PRESS":
            param = _radius_param(self._chunk, self._desc)
            if param is not None:
                param.value = _ring["radius"]
            self._resize_from = None
        elif event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            _ring["radius"] = start_radius
            self._resize_from = None
            _redraw()
        return {"RUNNING_MODAL"}


_CLASSES = (USDCHUNKS_OT_brush,)

_keymaps: list = []


def _register_keymap() -> None:
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:  # background Blender has no addon keyconfig
        return
    km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
    _keymaps.append((km, km.keymap_items.new(USDCHUNKS_OT_brush.bl_idname, "B", "PRESS")))


def _unregister_keymap() -> None:
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()


def register() -> None:
    for cls in _CLASSES:
        if not cls.is_registered:
            bpy.utils.register_class(cls)
    if _draw_state["handle"] is None:
        _draw_state["handle"] = bpy.types.SpaceView3D.draw_handler_add(
            _draw_ring, (), "WINDOW", "POST_VIEW")
    _register_keymap()


def unregister() -> None:
    _unregister_keymap()
    if _draw_state["handle"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_state["handle"], "WINDOW")
        _draw_state["handle"] = None
    _ring.clear()
    for cls in reversed(_CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
