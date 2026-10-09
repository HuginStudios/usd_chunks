"""A live Hydra Storm view of the chunk stage, in Blender's own 3D viewport.

It lives in the add-on rather than in an external launcher: the add-on always has a real
usd_chunks import available to it, and chunk_blender_addon.BlenderScene.get_selected_prims()
(viewport prim-picking) can read viewport state directly.

**Why a draw handler and not a RenderEngine.** A `bpy.types.RenderEngine` renders
Blender's depsgraph; getting an external stage in means importing it, which converts USD
to Blender data and throws away the reason to use a Hydra delegate at all. A
`SpaceView3D` draw handler calls `UsdImagingGL` against the real stage, in Blender's own
viewport GL context, with Blender's own camera -- no copy, no conversion, and the result
depth-composites with whatever Blender itself is drawing.

**The exception that is not an error.** `HgiGL` reports whatever GL errors are PENDING in
the queue -- including ones Blender's own drawing left there -- from a destructor that runs
AFTER the draw. On some hardware Blender's own
`hydra_storm` add-on emits the same message while rendering perfectly. So `Render()` is
wrapped and the exception dropped: catching it is the difference between a working
viewport and one that looks broken. If Hydra genuinely fails, the viewport goes empty --
that, not the message, is the signal.
"""
from __future__ import annotations

import logging
import traceback

import bpy
from bpy_extras import view3d_utils
from pxr import Gf, Glf, Sdf, Tf, Usd, UsdGeom, UsdImagingGL

from usd_chunks.frontends.blender import nodegraph as bg

logger = logging.getLogger(__name__)

#: Handler + engine live here rather than on an operator: Blender constructs operators per
#: click, and the UsdImagingGL engine must outlive a frame (rebuilding it per draw throws
#: away Hydra's whole cache and makes navigation crawl).
_state = {"handle": None, "engine": None, "stage_id": None, "error": "",
          "lighting": True, "draw_mode": "smooth", "renderer": None, "picking": True,
          "renderParams": None, "gl_pick_failed": False}

#: The draw mode names the panel offers, and what each maps to on UsdImagingGL.RenderParams.
#: Resolved lazily (inside _draw, not at import time) because UsdImagingGL.DrawMode needs
#: pxr already imported, and this module must not require that just to register its classes.
_DRAW_MODE_ATTRS = {
    "smooth": "DRAW_SHADED_SMOOTH",
    "flat": "DRAW_SHADED_FLAT",
    "wireframe": "DRAW_WIREFRAME",
    "wire_on_shaded": "DRAW_WIREFRAME_ON_SURFACE",
}

#: (keymap, keymap_item) pairs, so unregister can remove exactly what register added.
_keymaps: list = []


def active_stage():
    """The stage the open chunk network is authoring into, or None.

    Resolved every draw rather than captured: opening another network replaces the view,
    and a captured stage would leave the viewport showing the previous one.
    """
    view = bg.active_view()
    return None if view is None else view.stage


def current_time() -> Usd.TimeCode:
    """Blender's timeline frame, subframe included, as the time the stage is drawn at.

    Blender's frame numbers are used as USD time codes directly, the way usdview's
    timeline is: a stage authored at 24 time codes per second lines up with a 24 fps
    scene. Default time with no scene, so a headless caller still gets the static values.
    """
    scene = bpy.context.scene
    if scene is None:
        return Usd.TimeCode.Default()
    return Usd.TimeCode(scene.frame_current + scene.frame_subframe)


def _headlight(engine, rv3d) -> None:
    """A camera-relative light, the same "Camera Light" usdview defaults to -- with
    enableLighting on and no light authored anywhere on the stage, Storm has nothing to
    shade with at all and draws flat black. Recomputed every draw so it tracks navigation,
    same as the camera state below it.
    """

    eye = rv3d.view_matrix.inverted().translation
    light = Glf.SimpleLight()
    light.position = Gf.Vec4f(eye.x, eye.y, eye.z, 1.0)
    light.ambient = Gf.Vec4f(0.2, 0.2, 0.2, 1.0)
    light.diffuse = Gf.Vec4f(1.0, 1.0, 1.0, 1.0)
    light.specular = Gf.Vec4f(1.0, 1.0, 1.0, 1.0)

    material = Glf.SimpleMaterial()
    material.ambient = Gf.Vec4f(0.2, 0.2, 0.2, 1.0)
    material.diffuse = Gf.Vec4f(0.8, 0.8, 0.8, 1.0)
    material.specular = Gf.Vec4f(0.2, 0.2, 0.2, 1.0)
    material.shininess = 32.0

    engine.SetLightingState([light], material, Gf.Vec4f(0.1, 0.1, 0.1, 1.0))


def pick_matrix(ndc_x: float, ndc_y: float, dx: float, dy: float):
    """The classic pick matrix: move the click point to the origin, then blow the small
    window around it up to fill the whole [-1, 1] clip range.

    Its own function because it is one of the two parts of picking that cannot be checked
    without a GL context, and both are better pinned by a test than trusted by eye.
    Post-multiplied onto the projection by the caller, because Gf uses row-vectors
    (v * M1 * M2 applies M1 first).
    """
    return (Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(-ndc_x, -ndc_y, 0.0))
            * Gf.Matrix4d(1.0).SetScale(Gf.Vec3d(1.0 / dx, 1.0 / dy, 1.0)))


def hit_path(result) -> str | None:
    """The prim path out of whatever TestIntersection returned, or None for a miss.

    Scanned rather than indexed: the out-params (hit point, normal, prim path, instancer
    path, instance index, ...) have gained members across USD versions, so the position of
    outHitPrimPath is not something to trust blind -- but there is exactly one non-empty
    Sdf.Path among them, and it is the prim.

    The first element is the hit flag and is checked for falseness only when it is a bool:
    a build that returns the hit point there would have a Vec3d at the origin read as a
    miss, which is a hit dead centre of the near plane.
    """
    if not result:
        return None
    if isinstance(result[0], bool) and not result[0]:
        return None
    for item in result:
        if isinstance(item, Sdf.Path) and not item.isEmpty:
            return str(item)
    return None


def ray_from_region(region, rv3d, mouse_x, mouse_y):
    """The world-space ray under the cursor, as (origin, direction).

    Blender's own view maths, no GL: this is what the bounds pick below rides on, and the
    reason that pick keeps working on a driver where Hydra's does not.
    """
    coord = (mouse_x, mouse_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    return tuple(origin), tuple(direction)


def pick_by_bounds(stage, origin, direction, time=None) -> str | None:
    """The nearest prim whose world bounding box the ray enters, or None.

    The fallback for when Hydra's own pick is unavailable -- which on some builds is
    always, see `_pick_prim_path`. Pure USD and Gf, so it needs no GL context and can be
    tested; the cost is precision, since a box is not a mesh and a click just off a large
    object still lands on it.

    Only gprims are tested. An ancestor's box contains every child's, so including
    ancestors would return the scene root for every click; the leaf is what the user
    pointed at, and a host that wants the prim above it walks up from there (which is what
    the gizmo module does, to find the one a chunk drives).
    """
    if stage is None:
        return None
    time = Usd.TimeCode.Default() if time is None else time
    ray = Gf.Ray(Gf.Vec3d(*origin), Gf.Vec3d(*direction))
    cache = UsdGeom.BBoxCache(time, [UsdGeom.Tokens.default_, UsdGeom.Tokens.proxy,
                                     UsdGeom.Tokens.render])
    best_path, best_distance = None, None
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Gprim):
            continue
        imageable = UsdGeom.Imageable(prim)
        if imageable and imageable.ComputeVisibility(time) == UsdGeom.Tokens.invisible:
            continue
        box = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        if box.IsEmpty():
            continue
        hit, enter, _exit = ray.Intersect(box)
        if not hit:
            continue
        # A ray starting inside a box enters behind the camera; the near face is still what
        # was clicked, so clamp rather than skip.
        distance = max(enter, 0.0)
        if best_distance is None or distance < best_distance:
            best_path, best_distance = str(prim.GetPath()), distance
    return best_path


def _pick_prim_path(engine, stage, rv3d, region, mouse_x, mouse_y):
    """The prim under (mouse_x, mouse_y).

    Hydra's own pick first (UsdImagingGL.Engine.TestIntersection, the API usdview's
    viewport click-select uses), because it is exact -- the actual surface under the
    cursor, not a box around it. Narrows the projection to a few pixels around the click
    (pick_matrix) rather than building a Gf.Frustum, and reads the answer back with
    hit_path.

    **It is allowed to fail, and on some builds always does.** HgiGL reports whatever GL
    errors are pending in the queue -- including ones Blender's own drawing left there --
    and posts them as TfErrors, which Boost.Python raises at the Python boundary. The draw
    path swallows exactly this (see the module docstring); here the return value is the
    point of the call, so swallowing it leaves nothing. Hence the fallback: a CPU ray
    against prim bounds, which needs no GL and cannot be spoiled by someone else's error
    queue. Blender 5.1 / pxrBlender_v25_08, for one, raises an `invalid value` from
    HgiGLTexture's constructor on a viewport that is drawing perfectly.
    """

    ndc_x = (mouse_x / region.width) * 2.0 - 1.0
    ndc_y = (mouse_y / region.height) * 2.0 - 1.0
    # Half-width of the pick window, in NDC units -- a few pixels of tolerance either side.
    dx = 4.0 / region.width
    dy = 4.0 / region.height

    view = Gf.Matrix4d(*[v for row in rv3d.view_matrix.transposed() for v in row])
    proj = Gf.Matrix4d(*[v for row in rv3d.window_matrix.transposed() for v in row])
    narrowed_proj = proj * pick_matrix(ndc_x, ndc_y, dx, dy)

    path = None
    try:
        result = engine.TestIntersection(
            view,
            narrowed_proj,
            stage.GetPseudoRoot(),
            _state["renderParams"],
        )
        path = hit_path(result)
    except Tf.ErrorException:
        # Once, not per click: the queue is the driver's and will say the same thing every
        # time, and a warning per mouse-click is its own kind of broken.
        if not _state.get("gl_pick_failed"):
            _state["gl_pick_failed"] = True
            logger.info("Hydra picking unavailable on this build (pending GL errors); "
                        "falling back to bounds picking, which is less precise.")
    except Exception:
        logger.debug("Hydra pick failed", exc_info=True)

    if path is not None:
        return path
    origin, direction = ray_from_region(region, rv3d, mouse_x, mouse_y)
    return pick_by_bounds(stage, origin, direction, current_time())


def _draw():
    context = bpy.context
    rv3d = getattr(context, "region_data", None)
    region = getattr(context, "region", None)
    stage = active_stage()
    if stage is None or rv3d is None or region is None:
        return

    try:
        # One engine per stage. A new stage means a new Hydra scene, so the old engine's
        # populated scene index would keep drawing the previous network.
        if _state["engine"] is None or _state["stage_id"] != id(stage):
            _state["engine"] = UsdImagingGL.Engine()
            _state["renderParams"] = UsdImagingGL.RenderParams()
            _state["stage_id"] = id(stage)
            if _state["renderer"]:
                try:
                    _state["engine"].SetRendererPlugin(_state["renderer"])
                except Exception:
                    pass

        engine = _state["engine"]
        # Blender's viewport camera drives Hydra, so navigation just works. Transposed
        # because mathutils is row-major and Gf is column-major.
        view = Gf.Matrix4d(*[v for row in rv3d.view_matrix.transposed() for v in row])
        proj = Gf.Matrix4d(*[v for row in rv3d.window_matrix.transposed() for v in row])

        params = _state["renderParams"]
        params.enableLighting = _state["lighting"]
        # The timeline drives the stage: animation, value clips and Skel Pose play here.
        params.frame = current_time()
        params.drawMode = getattr(UsdImagingGL.DrawMode,
                                  _DRAW_MODE_ATTRS[_state["draw_mode"]])

        engine.SetRenderViewport(Gf.Vec4d(0, 0, region.width, region.height))
        engine.SetCameraState(view, proj)
        if _state["lighting"]:
            _headlight(engine, rv3d)
        try:
            engine.Render(stage.GetPseudoRoot(), params)
        except Exception:
            # HgiGL flushing pending GL errors after the draw -- see the module docstring.
            pass
        _state["error"] = ""
    except Exception as exc:  # a real failure: report once, keep the viewport alive
        if _state["error"] != str(exc):
            _state["error"] = str(exc)
            traceback.print_exc()


def hydra_view_enabled() -> bool:
    return _state["handle"] is not None


def enable_hydra_view() -> None:
    if _state["handle"] is None:
        _state["handle"] = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), "WINDOW", "POST_VIEW")
        _redraw()


def disable_hydra_view() -> None:
    if _state["handle"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_state["handle"], "WINDOW")
        _state.update(handle=None, engine=None, stage_id=None)
        _redraw()


def _redraw() -> None:
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _on_frame_change(*_args) -> None:
    """Redraw on a frame change. Blender only redraws a 3D view whose own depsgraph
    changed, and a stage with no Blender objects animating would otherwise hold its frame.
    """
    if _state["handle"] is not None:
        _redraw()


class USDCHUNKS_OT_toggle_hydra_view(bpy.types.Operator):
    """Draw the chunk network's USD stage in this viewport with Hydra Storm"""

    bl_idname = "usd_chunks.toggle_hydra_view"
    bl_label = "Hydra Stage View"

    def execute(self, context):
        if hydra_view_enabled():
            disable_hydra_view()
            self.report({"INFO"}, "Hydra stage view off")
        else:
            enable_hydra_view()
            self.report({"INFO"}, "Hydra stage view on")
        return {"FINISHED"}


class USDCHUNKS_OT_set_draw_mode(bpy.types.Operator):
    """Storm's own draw modes -- the same set usdview's View menu offers."""

    bl_idname = "usd_chunks.set_draw_mode"
    bl_label = "Set Draw Mode"

    mode: bpy.props.EnumProperty(items=[(k, k, "") for k in _DRAW_MODE_ATTRS])

    def execute(self, context):
        _state["draw_mode"] = self.mode
        _redraw()
        return {"FINISHED"}


class USDCHUNKS_OT_toggle_lighting(bpy.types.Operator):
    """RenderParams.enableLighting -- off draws flat display colour only, no shading at
    all, which is Storm's own default and reads as broken until toggled once.
    """

    bl_idname = "usd_chunks.toggle_lighting"
    bl_label = "Toggle Lighting"

    def execute(self, context):
        _state["lighting"] = not _state["lighting"]
        _redraw()
        return {"FINISHED"}


class USDCHUNKS_OT_pick_prim(bpy.types.Operator):
    """Ctrl+Click in the viewport: pick the prim under the cursor from the live Hydra
    stage -- this editor's counterpart of usdview's own viewport click-select.

    Bound to Ctrl+LeftMouse rather than plain click (see the panel's Picking toggle): a
    bare LEFTMOUSE keymap item would compete with Blender's own click-select, box-select
    and tool interactions, all of which already own that button. Returns PASS_THROUGH
    always, so Blender's normal handling of the same click still runs -- this only adds a
    side effect, it never consumes the event.
    """

    bl_idname = "usd_chunks.pick_prim"
    bl_label = "Pick USD Prim"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return _state["picking"] and hydra_view_enabled()

    def invoke(self, context, event):
        rv3d = getattr(context, "region_data", None)
        region = getattr(context, "region", None)
        stage = active_stage()
        engine = _state.get("engine")
        if rv3d is None or region is None or stage is None:
            # Said out loud rather than silently passed through: "my click does nothing" is
            # otherwise unattributable. The engine is NOT required -- it is only needed for
            # the Hydra attempt, and bounds picking answers without one.
            logger.debug("pick: nothing to ask (rv3d=%s region=%s stage=%s)",
                         rv3d is not None, region is not None, stage is not None)
            self.report({"INFO"}, "no live viewport to pick from")
            return {"PASS_THROUGH"}
        try:
            path = _pick_prim_path(engine, stage, rv3d, region,
                                   event.mouse_region_x, event.mouse_region_y)
        except Exception:
            traceback.print_exc()
            return {"PASS_THROUGH"}
        bg.set_picked_prim_paths([path] if path else [])
        self.report({"INFO"}, f"picked {path}" if path else "nothing under cursor")
        return {"PASS_THROUGH"}


class USDCHUNKS_OT_toggle_picking(bpy.types.Operator):
    """Ctrl+Click viewport picking on/off, without disabling the Hydra view itself."""

    bl_idname = "usd_chunks.toggle_picking"
    bl_label = "Toggle Picking"

    def execute(self, context):
        _state["picking"] = not _state["picking"]
        return {"FINISHED"}


def _available_renderers() -> list:
    try:
        return list(UsdImagingGL.Engine.GetRendererPlugins())
    except Exception:
        return []


def _renderer_display_name(plugin_id) -> str:
    try:
        return UsdImagingGL.Engine.GetRendererDisplayName(plugin_id) or str(plugin_id)
    except Exception:
        return str(plugin_id)


class USDCHUNKS_OT_set_renderer(bpy.types.Operator):
    """Switch the Hydra render delegate drawing the stage.

    Blender's own runtime currently exposes only HdStorm (measured:
    UsdImagingGL.Engine.GetRendererPlugins() == ['HdStormRendererPlugin']) -- there is no
    route to add hdVray/hdCycles/etc into it, since they are ABI-locked to whatever exact
    USD build produced them and Blender's is a differently-namespaced, monolithic one. This
    menu is future-proofed for whenever that changes rather than hardcoded to one entry.
    """

    bl_idname = "usd_chunks.set_renderer"
    bl_label = "Set Renderer"

    plugin: bpy.props.StringProperty()

    def execute(self, context):
        _state["renderer"] = self.plugin
        engine = _state.get("engine")
        if engine is not None:
            try:
                engine.SetRendererPlugin(self.plugin)
            except Exception:
                self.report({"WARNING"}, f"could not switch to {self.plugin}")
                return {"CANCELLED"}
        _redraw()
        return {"FINISHED"}


class USDCHUNKS_MT_renderer(bpy.types.Menu):
    bl_idname = "USDCHUNKS_MT_renderer"
    bl_label = "Renderer"

    def draw(self, context):
        layout = self.layout
        plugins = _available_renderers()
        if not plugins:
            layout.label(text="no delegates available")
            return
        for plugin_id in plugins:
            op = layout.operator(USDCHUNKS_OT_set_renderer.bl_idname,
                                 text=_renderer_display_name(plugin_id),
                                 depress=(_state["renderer"] == str(plugin_id)))
            op.plugin = str(plugin_id)


class USDCHUNKS_PT_hydra_view(bpy.types.Panel):
    """N-panel in the 3D viewport."""

    bl_idname = "USDCHUNKS_PT_hydra_view"
    bl_label = "USDChunks Stage"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "USDChunks"

    def draw(self, context):
        layout = self.layout
        on = hydra_view_enabled()
        layout.operator(USDCHUNKS_OT_toggle_hydra_view.bl_idname,
                        text="Turn Off" if on else "Turn On",
                        icon="HIDE_OFF" if on else "HIDE_ON",
                        depress=on)

        layout.label(text="Shading")
        row = layout.row(align=True)
        for mode, label in (("smooth", "Smooth"), ("flat", "Flat"),
                            ("wireframe", "Wire"), ("wire_on_shaded", "Wire+Shaded")):
            op = row.operator(USDCHUNKS_OT_set_draw_mode.bl_idname, text=label,
                              depress=(_state["draw_mode"] == mode))
            op.mode = mode
        layout.operator(USDCHUNKS_OT_toggle_lighting.bl_idname,
                        text="Lighting On" if _state["lighting"] else "Lighting Off",
                        icon="LIGHT" if _state["lighting"] else "LIGHT_DATA",
                        depress=_state["lighting"])
        layout.operator(USDCHUNKS_OT_toggle_picking.bl_idname,
                        text="Picking: Ctrl+Click" if _state["picking"] else "Picking Off",
                        icon="EYEDROPPER" if _state["picking"] else "PANEL_CLOSE",
                        depress=_state["picking"])
        # By name, not by class: brush.py imports this module. Greyed out until a brush
        # chunk is selected in the graph or picked here.
        layout.operator("usd_chunks.brush", text="Paint (B)", icon="BRUSH_DATA")

        current = _state["renderer"]
        label = _renderer_display_name(current) if current else "default"
        layout.menu(USDCHUNKS_MT_renderer.bl_idname, text=f"Renderer: {label}")

        stage = active_stage()
        layout.label(text="stage: %s" % ("open" if stage is not None else "no network"),
                     icon="FILE" if stage is not None else "ERROR")
        if _state["error"]:
            layout.label(text=_state["error"][:60], icon="ERROR")


_CLASSES = (USDCHUNKS_OT_toggle_hydra_view, USDCHUNKS_OT_set_draw_mode,
            USDCHUNKS_OT_toggle_lighting, USDCHUNKS_OT_pick_prim,
            USDCHUNKS_OT_toggle_picking, USDCHUNKS_OT_set_renderer,
            USDCHUNKS_MT_renderer, USDCHUNKS_PT_hydra_view)


def _register_keymap() -> None:
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:  # background/headless Blender has no addon keyconfig
        return
    km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
    kmi = km.keymap_items.new(USDCHUNKS_OT_pick_prim.bl_idname, "LEFTMOUSE", "PRESS",
                              ctrl=True)
    _keymaps.append((km, kmi))


def _unregister_keymap() -> None:
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()


def register() -> None:
    for cls in _CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:  # already registered by a previous run of this script
            pass
    _register_keymap()
    # ON by default: showing the live stage is the whole point of this module. It costs
    # nothing until a network is open, since _draw returns immediately with no stage.
    enable_hydra_view()
    if _on_frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_on_frame_change)
    logger.info("Hydra stage view ON -- 3D viewport > N panel > USDChunks to toggle")


def unregister() -> None:
    if _on_frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_on_frame_change)
    disable_hydra_view()
    _unregister_keymap()
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # pragma: no cover - already gone
            pass
