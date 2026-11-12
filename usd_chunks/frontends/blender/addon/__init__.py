"""The Blender add-on: what Blender enables, and what it wires up.

`chunk_blender_nodegraph` is the frontend and `chunk_blender` the widgets; neither knows
anything about being installed. This package is the part Blender actually loads -- a
manifest, `register()`/`unregister()`, a SceneSender, and the operators that open a network.

**A node tree corresponds to a `.usda` file.** Blender has no ambient USD stage the way
usdview does, so something has to say which stage a graph edits. Tying it to a file the
user opens is the honest answer: it matches what `usdchunks run` takes, it makes the tree a
view of something that exists on disk, and it means saving the `.blend` never becomes a way
of saving the graph. The path is kept on the tree itself (`chunk_network_path`), so two
trees can edit two networks.

**Where it appears.** The node editor's editor-type dropdown gains "USDChunks Editor",
alongside Geometry Node Editor and the rest; the sidebar gets a USDChunks tab, and Shift+A
lists the whole chunk library by the namespace each chunk declares.

**Installing.** Blender 4.2+ extension, built with `blender --command extension build`.
It needs `usd_chunks` importable, installed with `pip --target` into Blender's user
`scripts/addons/modules` folder. See docs/blender.md for the steps.

Do **not** install `usd-core` alongside. Blender already ships the bindings, under a
private namespace (`pxrBlender_v26_03__pxrReserved__`), so a pip build is a different C++
type and cannot exchange objects with Blender's. That is why `usd-core` is an optional
extra of the package rather than a dependency.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import os
import tempfile
import threading
import time

import bpy
from bpy.app.handlers import persistent
from pxr import Sdf, Usd

from usd_chunks import chunk_registry, configure_logging
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.frontends.blender import delegates
from usd_chunks.frontends.blender import nodegraph as bg
from usd_chunks.graph import engine as chunk_graph
from usd_chunks.frontends.blender.addon import brush
from usd_chunks.frontends.blender.addon import camera
from usd_chunks.frontends.blender.addon import dependencies
from usd_chunks.frontends.blender.addon import gizmos
from usd_chunks.frontends.blender.addon import undo
from usd_chunks.frontends.blender.addon import viewport

logger = logging.getLogger(__name__)


class BlenderScene(SceneSender):
    """The host interface, backed by Blender.

    Most of what a SceneSender exposes is about a *viewer* -- the active camera, the
    viewport renderer, the current frame -- and Blender has all of those, but a chunk graph
    editing a `.usda` on disk does not need them to be truthful to be useful. They answer
    honestly where Blender can and return a neutral value where the question does not apply,
    rather than raising: a chunk asking for the frame should get a number, not an exception
    that stops a cook.
    """

    is_app = True

    def __init__(self, stage=None):
        super().__init__()
        self._stage = stage

    # -- the two that actually matter -------------------------------------

    def get_stage(self):
        return self._stage

    def get_host(self) -> tuple[str, str]:
        return "blender", ".".join(str(part) for part in bpy.app.version)

    def call_later(self, fn, delay: float = 0.0):
        """Queued for _run_due_calls, a timer on the main thread.

        Not bpy.app.timers.register directly: that is not safe from a worker thread, and a
        finished download calling back from one is the main reason this exists.
        """
        cancelled = []
        with _later_lock:
            heapq.heappush(_later, (time.monotonic() + delay, next(_later_order), fn, cancelled))
        return lambda: cancelled.append(True)

    def set_stage(self, stage) -> None:
        self._stage = stage

    def get_selected_prims(self):
        """The prim(s) last Ctrl+Click-picked in the live Hydra viewport.

        NOT Blender's own object selection: a USD prim shown by the live draw handler
        (viewport.py) has no Blender object at all to select in the first place, so that
        road is a dead end -- this instead reads
        chunk_blender_nodegraph.get_picked_prim_paths(), written by viewport.py's
        USDCHUNKS_OT_pick_prim from its own UsdImagingGL.Engine.TestIntersection call.
        """
        if self._stage is None:
            return []
        prims = []
        for path in bg.get_picked_prim_paths():
            prim = self._stage.GetPrimAtPath(path)
            if prim and prim.IsValid():
                prims.append(prim)
        return prims

    def get_selected_property(self):
        return None

    # -- viewer questions, answered where Blender can ---------------------

    def get_active_camera(self):
        """The 3D view being navigated -- Blender's free camera -- lens included."""
        return camera.viewport_camera()

    def set_viewport_camera(self, prim) -> None:
        camera.set_viewport_camera(prim, self)

    def release_viewport_camera(self) -> None:
        camera.release_viewport_camera()

    def look_at(self, point) -> None:
        camera.look_at(point)

    def get_viewport_renderer(self) -> str:
        scene = bpy.context.scene
        return str(scene.render.engine) if scene else ""

    def set_viewport_renderer_settings(self, settings) -> None:
        """Not applicable: Blender's render engines are not Hydra delegates taking USD
        render settings. Ignored rather than raised so a render chunk can still cook.
        """
        return None

    def get_current_frame(self) -> float:
        return float(bpy.context.scene.frame_current) if bpy.context.scene else 0.0

    def get_end_frame(self) -> float:
        return float(bpy.context.scene.frame_end) if bpy.context.scene else 0.0

    def get_start_frame(self) -> float:
        return float(bpy.context.scene.frame_start) if bpy.context.scene else 0.0

    # Blender's frames are whole numbers plus a subframe; set both so a fractional frame
    # a chunk asks for is the frame the Hydra view draws (viewport.current_time).

    def set_current_frame(self, frame: float):
        if bpy.context.scene:
            whole = int(frame // 1)
            bpy.context.scene.frame_set(whole, subframe=float(frame) - whole)

    def set_end_frame(self, frame: float):
        if bpy.context.scene:
            bpy.context.scene.frame_end = int(round(frame))

    def set_start_frame(self, frame: float):
        if bpy.context.scene:
            bpy.context.scene.frame_start = int(round(frame))

    def should_install_packages(self, packages, chunk_name: str) -> bool:
        """Offer the install in a moment, through a dialog; no for now.

        Blender cannot block on a question while it loads chunks -- see dependencies.py,
        which asks once the add-on is idle and registers the chunk's node type after a yes.
        Packages go to the per-version dependency dir, never into Blender's own Python.
        """
        dependencies.queue(packages, chunk_name)
        return False

    def run_install(self, packages, chunk_name: str, run):
        # Called on dependencies.py's worker thread (or inline, with USD_CHUNK_AUTO_INSTALL=1):
        # report_line only stores the text for the modal operator to show.
        return run(dependencies.report_line)


#: One scene and one view per Blender session. A second node tree re-points them rather
#: than making a second set, because the chunk registry is a singleton keyed to one scene --
#: two live scenes would have the registry answering for whichever initialised last.
_scene: BlenderScene | None = None


#: call_later's queue: (due, order, fn, cancelled), drained on the main thread.
_later: list = []
_later_lock = threading.Lock()
_later_order = itertools.count()

#: How often the queue is checked: the resolution of a call_later delay.
_LATER_INTERVAL = 0.05
#: The soonest the queue is checked again when a call is about due.
_LATER_MIN = 0.001


def _run_due_calls() -> float:
    now = time.monotonic()
    due = []
    with _later_lock:
        while _later and _later[0][0] <= now:
            due.append(heapq.heappop(_later))
    for _when, _order, fn, cancelled in due:
        if cancelled:
            continue
        try:
            fn()
        except Exception:  # one failing call must not stop the queue for good
            logger.exception("usd_chunks: a call_later callback failed")
    # Wake for the next call when it is due, not at the next poll: a gizmo drag's held-back
    # write is due within a frame or two, and a 50 ms poll would add up to 50 ms to it.
    with _later_lock:
        if _later:
            return min(_LATER_INTERVAL, max(_LATER_MIN, _later[0][0] - time.monotonic()))
    return _LATER_INTERVAL


def get_view():
    return bg.active_view()


def _open_stage(stage, context_path=None, tree=None) -> None:
    """Point the session at `stage` and rebuild the tree from it.

    `tree` overrides the usual `_active_tree()` guess -- needed by `_poll_new_trees`
    below, whose caller already knows exactly which tree it found stale and cannot trust
    `_active_tree()` (or `bpy.context.space_data` generally) to point at the same one.
    """
    global _scene
    current = get_view()
    if current is not None and tree is not None and current.tree is not None and current.tree != tree:
        _park(current)
    _scene = BlenderScene(stage)
    camera.attach(_scene)
    chunk_registry.ChunkRegistry().initialize(_scene, "blender")
    chunk_registry.ChunkLayerManager().initialize(stage)
    # ChunkStageSync.initialize loads the file's chunks itself, with the dedup set that
    # stops a prim loading twice. A second load_chunks after it -- which stood here --
    # had no such set and loaded every chunk again: each got a fresh prim (Reference ->
    # Reference8, View1 -> View11) laid out in a row off to the right of the real
    # network, which read as "the file's positions are wrong".
    chunk_stage.ChunkStageSync().initialize(stage, _scene)
    # Node classes are generated from the registry, and the registry only discovers chunks
    # once a scene exists -- so this cannot happen in register(), where there is no scene
    # yet and every class would be generated from an empty library. Idempotent, so opening
    # a second network costs a dict lookup per already-registered type.
    made = bg.register_chunk_types()
    if made:
        logger.info("usd_chunks: %d chunk node type(s) registered", len(made))

    if tree is None:
        tree = _active_tree()
    if tree is not None:
        # Marks this tree as USD-backed, so _poll_new_trees below knows not to
        # re-initialize it (which would replace whatever stage/content it already has)
        # the next time the user merely switches back to looking at it.
        tree.chunk_network_initialized = True
    chunks = {chunk_stage.get_chunk_prim_path(c): c for c in _scene.get_chunks()}
    # chunk_graph.default_context, not peek_root_context: after load_chunks the scene has
    # no root context to peek at, and CHUNKS_ROOT shows the default chunkgroup and none of
    # the work inside it. The engine already knows which graph a file wants opened.
    context = context_path or bg.chunk_graph.default_context(stage)
    view = bg.BlenderGraphView(stage, _scene, tree=tree, context=context)
    view.chunks = chunks
    # Read by the Open operator's report and the sidebar panel: a chunk type that no
    # module registers was once a log line only, and the network looked half-empty.
    view.missing_types = bg.missing_chunk_types(stage)
    view.reload()
    # A context with no view cooks nothing, so this is not cosmetic -- see ensure_view.
    view.ensure_view()
    # From here on the network's USD rides along with Blender's undo steps.
    undo.track(view.tree, stage)
    if view.tree is not None:
        _frame_soon(view.tree)


def _frame_soon(tree) -> None:
    """Fit the whole graph in every node editor showing `tree`, once it has drawn.

    Saved positions are wherever the file put them, often thousands of units from the
    origin the editor starts at, so a freshly opened network looked like an empty editor
    until Home was pressed. Deferred: a node's size is only known after its first draw,
    and view_all frames nothing before that.
    """
    name = tree.name
    tries = [50]  # 0.1 s apart: a slow first draw (a big library loading) gets 5 s

    def frame():
        tree_now = bpy.data.node_groups.get(name)
        if tree_now is None:
            return None
        if not any(n.dimensions[0] for n in tree_now.nodes):
            tries[0] -= 1
            return 0.1 if tries[0] > 0 else None
        for window in getattr(bpy.context.window_manager, "windows", ()):
            for area in window.screen.areas:
                if area.type != "NODE_EDITOR" or getattr(area.spaces.active, "node_tree", None) != tree_now:
                    continue
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if region is None:
                    continue
                try:
                    with bpy.context.temp_override(window=window, area=area, region=region):
                        bpy.ops.node.view_all()
                except RuntimeError:
                    logger.debug("usd_chunks: could not frame %s", name, exc_info=True)
        return None

    if not bpy.app.background:
        bpy.app.timers.register(frame, first_interval=0.2)


# ---------------------------------------------------------------------------
# Catching Blender's own "+ New" (bpy.ops.node.new_node_tree)
# ---------------------------------------------------------------------------
#
# Blender always draws its stock ID-template "+ New" / browse widget in a node editor's
# header, for every registered tree type including ours -- there is no per-type opt-out,
# and overriding NODE_HT_header.draw wholesale risks fighting Blender's own View/Select/
# Add/Node menus across versions. That widget calls bpy.ops.node.new_node_tree(), which
# knows nothing about USD: it leaves a blank tree with no stage, no scene, no view behind
# it -- nothing executes (the Add menu comes from the registry, which only a real
# _open_stage populates), and switching away and back "loses" the ORIGINAL network's
# connections because active_view() is a single global and would point at this second,
# empty view instead.
#
# bpy.msgbus.subscribe_rna on SpaceNodeEditor.node_tree does not fire reliably for this,
# because a msgbus callback runs with whatever bpy.context Blender happens to
# have at notification time, which is not guaranteed to be the node editor whose property
# actually changed. A timer callback's context has the same problem, so this does not
# trust bpy.context.space_data either -- it scans every NODE_EDITOR area directly, the way
# viewport.py's _redraw() already does for VIEW_3D areas.

def _uninitialized_chunk_trees() -> list:
    """Every ChunkNetwork tree currently shown in a node editor that _open_stage has never
    touched -- i.e. created by Blender's own "+ New" rather than one of our operators.
    """
    found = []
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type != "NODE_EDITOR":
                continue
            space = area.spaces.active
            tree = getattr(space, "node_tree", None)
            if (tree is not None and tree.bl_idname == bg.TREE_ID
                    and not tree.chunk_network_initialized and tree not in found):
                found.append(tree)
    return found


def _poll_new_trees() -> float:
    try:
        for tree in _uninitialized_chunk_trees():
            # A tree saved by a session before chunk_network_initialized existed defaults to
            # False too -- but if it already names a file, it is not blank, it just has not
            # been touched THIS session. Reopen the file rather than blanking a real network.
            _open_stage(_stage_for_tree(tree), tree=tree)
        _follow_shown_tree()
    except Exception:  # a timer that raises is unregistered for good
        logger.exception("usd_chunks: could not switch to the chunk network on screen")
    return 0.1  # reschedule in 0.1s -- this is the latency between "+ New" and Shift+A working


#: Edits to a network with a file, kept while another network is live: the file is the
#: user's, so switching away never writes it. Keyed by nodegraph.tree_uid.
_parked_edits: dict = {}

#: The graph each parked network was showing (a chunkgroup it was dived into).
_parked_context: dict = {}


def _park(view) -> None:
    """Keep the live network's state with its tree before the session moves to another.

    One network is live at a time (one stage, one scene, one view), so the outgoing one is
    stored the way a .blend save stores it: in the tree when it has no file, and here when
    its file is behind what is on screen. Without this, New dropped an unsaved network's
    edits, and coming back to a network reopened what it was before.
    """
    tree = view.tree
    if tree is None:
        return
    uid = bg.tree_uid(tree)
    try:
        _parked_context[uid] = view.context
        if not tree.chunk_network_path:
            tree.chunk_network_embedded = network_text(view)
        elif network_unsaved(view):
            _parked_edits[uid] = network_text(view)
        else:
            _parked_edits.pop(uid, None)
    except Exception:
        logger.exception("usd_chunks: could not keep the edits of %s", tree.name)


def _shown_chunk_trees() -> list:
    """The USD-backed chunk trees node editors are showing, in screen order."""
    found = []
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type != "NODE_EDITOR":
                continue
            tree = getattr(area.spaces.active, "node_tree", None)
            if (tree is not None and tree.bl_idname == bg.TREE_ID
                    and tree.chunk_network_initialized and tree not in found):
                found.append(tree)
    return found


def _follow_shown_tree() -> None:
    """Make the network on screen the live one, when the editor has been switched to it.

    Picking another chunk tree in the editor's browse menu changed only what Blender drew:
    the session stayed on the newest network, so the older one showed nodes that nothing
    cooked, and a chunk added there went into the other network's stage.
    """
    shown = _shown_chunk_trees()
    view = get_view()
    if not shown or (view is not None and view.tree in shown):
        return
    tree = shown[0]
    _open_stage(_stage_for_tree(tree), context_path=_parked_context.get(bg.tree_uid(tree)), tree=tree)


def _stage_for_tree(tree):
    """The stage a tree names: its file (with any parked edits on top), else the network
    embedded in the .blend, else new."""
    if tree.chunk_network_path:
        stage = Usd.Stage.Open(bpy.path.abspath(tree.chunk_network_path))
        if stage is not None:
            edits = _parked_edits.pop(bg.tree_uid(tree), None)
            if edits is not None:
                # Into the file's own layer, so its relative paths still resolve; the layer
                # is left dirty, so a .blend save still warns about it.
                stage.GetRootLayer().ImportFromString(edits)
            return stage
        logger.warning("usd_chunks: could not open %s", tree.chunk_network_path)
    if tree.chunk_network_embedded:
        layer = Sdf.Layer.CreateAnonymous(".usda")
        if layer.ImportFromString(tree.chunk_network_embedded):
            return Usd.Stage.Open(layer)
        logger.warning("usd_chunks: the network stored in %s did not parse", tree.name)
    return new_network_stage()


def blender_space() -> tuple[str, float]:
    """(up axis, meters per unit) of Blender's scene: Z-up, metres times the Unit Scale."""
    scene = getattr(bpy.context, "scene", None)
    scale = getattr(getattr(scene, "unit_settings", None), "scale_length", 1.0) or 1.0
    return "Z", float(scale)


def stamp_space(stage) -> None:
    """Author Blender's space on stage's root layer, where the layer does not say its own.

    A network is opened in other hosts too, and each turns it from the space it was
    authored in into its own -- Houdini's chunkgroups do (hosts/houdini/space.py). That
    space is the root layer's upAxis and metersPerUnit, as usdview's save stamps them. A
    network with neither reads as USD's fallbacks, Y-up centimetres, which for one made
    in Blender is wrong both ways. A file that already says is left as it says: an opened
    network keeps the space it was authored in.
    """
    up, meters = blender_space()
    info = stage.GetRootLayer().pseudoRoot
    if not info.HasInfo("upAxis"):
        info.SetInfo("upAxis", up)
    if not info.HasInfo("metersPerUnit"):
        info.SetInfo("metersPerUnit", meters)


def new_network_stage():
    """An empty stage for a network started in Blender, in Blender's space."""
    stage = Usd.Stage.CreateInMemory()
    stamp_space(stage)
    return stage


def write_network(view, path: str) -> None:
    """Write the open network to `path`, positions and space included, as usdview saves one.

    Every chunk prim, wherever it is authored (chunk_stage.save_scene). Writing the root
    layer alone lost most of a network: on load, chunk prims move into the session layer
    and into the layers Edit Target chunks name, and a test_shot3 network saved that way
    kept 2 of its 22 chunks.
    """
    # Blender owns dragging, so there is no move event to hook; positions are collected
    # here instead. Without this a layout done in Blender is lost, because the tree is
    # a view and the next reload overwrites it from USD.
    view.pull_positions_from_tree()
    # And its space, for a network that never had one written (see stamp_space).
    stamp_space(view.stage)
    chunk_stage.save_scene(view.scene, os.path.abspath(bpy.path.abspath(path)))


def network_text(view) -> str:
    """The open network as .usda text, as write_network writes it, for one with no file."""
    view.pull_positions_from_tree()
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "network.usda")
        chunk_stage.export_chunks(view.scene, path)
        with open(path, encoding="utf-8") as f:
            return f.read()


def network_unsaved(view) -> bool:
    """Whether the open network's file is behind what is on screen.

    Reads the root layer's dirty flag, so node positions are pulled into it first --
    a drag is otherwise invisible to USD until Save.
    """
    view.pull_positions_from_tree()
    return view.stage.GetRootLayer().dirty


def _warn_unsaved(name: str) -> None:
    """A popup, after the save: save_pre runs mid-save, where no UI can be drawn."""
    def draw(menu, _context):
        menu.layout.label(text=f"{name} has edits the .blend does not save.")
        menu.layout.operator(USDCHUNKS_OT_save_network.bl_idname, icon="FILE_TICK")

    wm = bpy.context.window_manager
    if wm is not None and wm.windows:
        wm.popup_menu(draw, title="Unsaved chunk network", icon="ERROR")


@persistent
def _on_save_pre(*_args) -> None:
    """Saving the .blend warns about an edited network; it never writes the .usda.

    The .usda is the model and the tree only a view of it, so a Ctrl+S that kept the tree
    but silently dropped the network's edits would lose the half that matters on reopen.
    Writing the file unasked is no better -- it is the user's, and may be shared -- so a
    network with a file is only checked, and the user is told. One without a file
    (started with New, never saved) has nothing to overwrite: it is stored in the tree
    itself, and _stage_for_tree reopens it from there.
    """
    for parked in [t for t in bpy.data.node_groups
                   if t.bl_idname == bg.TREE_ID and t.get(bg.TREE_UID) in _parked_edits]:
        name = bpy.path.basename(parked.chunk_network_path)
        logger.warning("usd_chunks: %s has unsaved edits; the .blend does not save them", name)
        bpy.app.timers.register(lambda name=name: _warn_unsaved(name), first_interval=0.0)
    view = get_view()
    tree = view.tree if view is not None else None
    if tree is None:
        return
    try:
        if not tree.chunk_network_path:
            tree.chunk_network_embedded = network_text(view)
        elif network_unsaved(view):
            name = bpy.path.basename(tree.chunk_network_path)
            logger.warning("usd_chunks: %s has unsaved edits; the .blend does not save them",
                           name)
            bpy.app.timers.register(lambda: _warn_unsaved(name), first_interval=0.0)
    except Exception:  # never stop the .blend from saving
        logger.exception("usd_chunks: could not check the chunk network on .blend save")


#: Seconds Blender's scene must sit still before chunks hear it changed: a drag or a
#: slider scrub is one change, not one per redraw.
_HOST_CHANGE_DEBOUNCE = 0.25

#: The module hook a chunk defines to follow Blender's own scene: `host_changed(node)`.
#: The chunk decides what changed enough to cook (blender_import compares a fingerprint),
#: since only its module knows what it reads.
HOST_CHANGED_HOOK = "host_changed"


#: The module hook naming the host objects a chunk stands in for: `host_sources(node)`.
#: The add-on hides those in Blender's viewport, so an import is not drawn twice.
HOST_SOURCES_HOOK = "host_sources"

#: On the Blender scene: the objects the add-on hid, so it unhides only those -- never one
#: the user hid -- and still knows them after the .blend is reopened.
_HIDDEN_SOURCES = "usd_chunks_hidden_sources"

#: How often sources are reconciled. A timer rather than events, because what changes the
#: answer is scattered: a chunk deleted, disabled, re-pointed at another collection, or a
#: view switched.
_SOURCES_INTERVAL = 0.5


def _sync_host_sources() -> float:
    """Hide what some chunk's host_sources claims; unhide what the add-on hid and none does."""
    try:
        scene = bpy.context.scene
        if scene is None:
            return _SOURCES_INTERVAL
        claimed = set()
        for chunk in list(_scene.get_chunks()) if _scene is not None else ():
            hook = chunk._module_hook(HOST_SOURCES_HOOK)
            if hook is not None:
                claimed.update(hook(chunk) or ())
        view_layer = bpy.context.view_layer or scene.view_layers[0]
        hidden = set(scene.get(_HIDDEN_SOURCES, ()))
        changed = False
        for name in hidden - claimed:
            obj = bpy.data.objects.get(name)
            if obj is not None and obj.name in view_layer.objects:
                obj.hide_set(False, view_layer=view_layer)
            hidden.discard(name)
            changed = True
        for name in claimed - hidden:
            obj = bpy.data.objects.get(name)
            if obj is None or obj.name not in view_layer.objects or obj.hide_get(view_layer=view_layer):
                continue  # gone, or already hidden by the user: theirs to unhide
            obj.hide_set(True, view_layer=view_layer)
            hidden.add(name)
            changed = True
        if changed:
            scene[_HIDDEN_SOURCES] = sorted(hidden)
    except Exception:  # a timer that raises is unregistered for good
        logger.exception("usd_chunks: could not reconcile hidden source objects")
    return _SOURCES_INTERVAL


def _notify_host_changed():
    if _scene is None:
        return None
    for chunk in list(_scene.get_chunks()):
        hook = chunk._module_hook(HOST_CHANGED_HOOK)
        if hook is None:
            continue
        try:
            hook(chunk)
        except Exception:
            logger.exception("usd_chunks: %s failed on chunk %r", HOST_CHANGED_HOOK, chunk.name)
    return None  # one shot; the next change schedules the next


@persistent
def _on_depsgraph_update(_scene_arg, depsgraph) -> None:
    """Blender's scene changed: tell the chunks that follow it, once things settle.

    Only for changes to Blender data -- the camera mirror (camera.py) rewrites an object
    every time a USD camera moves, and that must not read as the user editing the scene.
    """
    if _scene is None:
        return
    ids = [update.id for update in depsgraph.updates]
    if ids and all(getattr(i, "name", "") == camera.MIRROR_NAME for i in ids):
        return
    if bpy.app.timers.is_registered(_notify_host_changed):
        bpy.app.timers.unregister(_notify_host_changed)
    bpy.app.timers.register(_notify_host_changed, first_interval=_HOST_CHANGE_DEBOUNCE)


@persistent
def _on_load_post(*_args) -> None:
    """Reopen the network a freshly loaded .blend names.

    `chunk_network_initialized` is saved with the tree, so after a load it reads True for
    a tree no stage is behind -- _poll_new_trees would skip it, and the previous file's
    view (and its stage, in the Hydra view) would stay active. Reset every flag, then open
    the first chunk tree straight away rather than waiting for an editor to show it.
    """
    trees = [t for t in bpy.data.node_groups if t.bl_idname == bg.TREE_ID]
    for tree in trees:
        tree.chunk_network_initialized = False
    camera.release_viewport_camera()
    # The previous file's view would otherwise be parked into this file's trees.
    bg.clear_active_view()
    _parked_edits.clear()
    _parked_context.clear()
    if not trees:
        return
    try:
        _open_stage(_stage_for_tree(trees[0]), tree=trees[0])
    except Exception:
        logger.exception("usd_chunks: could not reopen the chunk network in this .blend")


def _active_tree():
    space = getattr(bpy.context, "space_data", None)
    tree = getattr(space, "edit_tree", None) if space else None
    if tree is not None and tree.bl_idname == bg.TREE_ID:
        return tree
    return next((t for t in bpy.data.node_groups if t.bl_idname == bg.TREE_ID), None)


def _show_tree(context, tree) -> None:
    """Pin the node editor to `tree`.

    `space_data.edit_tree` (what `_active_tree` reads) is read-only -- it is derived from
    `node_tree` plus any node-group diving depth. Without this, New/Open would create or
    open a tree that nothing on screen points at: the editor would keep showing whatever
    tree (or nothing) it already had pinned, `_active_tree`'s fallback would pick among
    ChunkNetwork trees by iteration order, and a graph could be built into a tree the user
    is not looking at.
    """
    space = getattr(context, "space_data", None)
    if space is not None and hasattr(space, "node_tree"):
        space.node_tree = tree


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class USDCHUNKS_OT_open_network(bpy.types.Operator):
    """Open a .usda chunk network and mirror it into a node tree."""

    bl_idname = "usd_chunks.open_network"
    bl_label = "Open Chunk Network"
    # An undo step of its own: without one, Ctrl+Z after the first edit lands before the
    # open, where this tree did not exist yet, and the whole network goes.
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.usda;*.usd;*.usdc", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        stage = Usd.Stage.Open(self.filepath)
        if stage is None:
            self.report({"ERROR"}, f"could not open {self.filepath}")
            return {"CANCELLED"}
        tree = _active_tree()
        if tree is None:
            tree = bpy.data.node_groups.new("ChunkNetwork", bg.TREE_ID)
        tree.chunk_network_path = self.filepath
        tree.chunk_network_embedded = ""
        _show_tree(context, tree)
        _open_stage(stage, tree=tree)
        missing = missing_types_message(get_view())
        if missing:
            # WARNING, not ERROR: the open worked, and an ERROR report makes a scripted
            # bpy.ops call raise. It still shows in the status bar and Info log.
            self.report({"WARNING"}, f"opened {self.filepath}, but {missing}")
        else:
            self.report({"INFO"}, f"opened {self.filepath}")
        return {"FINISHED"}


def missing_types_message(view) -> str:
    """One line naming the chunk types the open network needs and no module registers,
    or "" when there are none."""
    missing = getattr(view, "missing_types", None) or {}
    if not missing:
        return ""
    count = sum(len(paths) for paths in missing.values())
    names = ", ".join(sorted(missing))
    return (f"{count} chunk(s) have no module ({names}) -- "
            "is their folder on USD_CHUNK_DIRS?")


# ---------------------------------------------------------------------------
# File > Import > USD
# ---------------------------------------------------------------------------

def has_chunk_network(stage) -> bool:
    """Whether `stage` carries a chunk network: a Chunk prim anywhere under /chunks."""
    root = stage.GetPrimAtPath(chunk_graph.CHUNKS_ROOT) if stage is not None else None
    if not root:
        return False
    return any(p.GetTypeName() == chunk_graph.CHUNK_TYPENAME for p in Usd.PrimRange(root))


def open_imported_network(path: str):
    """Open the network in the file Blender just imported, into a tree of its own.

    A new tree rather than the active one: an import adds to the scene, so it must not
    replace a network already open (_open_stage parks that one's edits). Every node editor
    showing a chunk tree, or none, is pointed at it, so the graph is on screen at once.
    Returns the tree, or None if the file could not be opened.
    """
    stage = Usd.Stage.Open(path)
    if stage is None:
        logger.warning("usd_chunks: could not open the imported network %s", path)
        return None
    tree = bpy.data.node_groups.new(bpy.path.display_name_from_filepath(path), bg.TREE_ID)
    tree.chunk_network_path = path
    tree.chunk_network_embedded = ""
    _open_stage(stage, tree=tree)
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type != "NODE_EDITOR":
                continue
            space = area.spaces.active
            if getattr(space, "tree_type", "") == bg.TREE_ID:
                space.node_tree = tree
                area.tag_redraw()
    missing = missing_types_message(get_view())
    if missing:
        logger.warning("usd_chunks: imported %s, but %s", path, missing)
        _popup(f"Imported {bpy.path.basename(path)}, but {missing}", "ERROR")
    else:
        logger.info("usd_chunks: opened the chunk network in %s", path)
    return tree


def _popup(message: str, icon: str) -> None:
    """A popup at the mouse: the import's own report is the importer's, not ours, so a
    problem with the network would otherwise be a console line only."""
    if bpy.app.background:
        return

    def draw(menu, _context):
        menu.layout.label(text=message)

    bpy.context.window_manager.popup_menu(draw, title="USDChunks", icon=icon)


#: Network files imported but not opened yet; see USDCHUNKS_usd_hook.
_pending_imports: list[str] = []


def _open_pending_imports():
    """Timer: open what on_import queued, now that the importer has finished."""
    while _pending_imports:
        path = _pending_imports.pop(0)
        try:
            open_imported_network(path)
        except Exception:  # a timer that raises is unregistered for good
            logger.exception("usd_chunks: could not open the imported network %s", path)
            _popup(f"Could not open the chunk network in {bpy.path.basename(path)}: "
                   "see the system console", "ERROR")
    return None


if hasattr(bpy.types, "USDHook"):
    class USDCHUNKS_usd_hook(bpy.types.USDHook):
        """File > Import > USD opens the file's chunk network in the node editor.

        Blender's importer makes objects from the prims and ignores the /chunks graph, so
        without this an imported network file showed no graph and cooked nothing. The open
        is deferred to a timer: on_import runs inside the importer's operator, mid-import,
        and the network gets a stage of its own opened from the file.
        """

        bl_idname = "usd_chunks_network"
        bl_label = "USDChunks network"
        bl_description = "Open the file's chunk network in the USDChunks node editor"

        @staticmethod
        def on_import(import_context):
            stage = import_context.get_stage()
            if not has_chunk_network(stage):
                return True
            path = stage.GetRootLayer().realPath
            if not path:
                return True
            _pending_imports.append(path)
            if not bpy.app.timers.is_registered(_open_pending_imports):
                bpy.app.timers.register(_open_pending_imports, first_interval=0.0)
            return True

    _USD_HOOKS: tuple = (USDCHUNKS_usd_hook,)
else:  # pragma: no cover - a Blender without USD hooks
    _USD_HOOKS = ()


class USDCHUNKS_OT_new_network(bpy.types.Operator):
    """Start an empty chunk network in a new node tree."""

    bl_idname = "usd_chunks.new_network"
    bl_label = "New Chunk Network"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        tree = bpy.data.node_groups.new("ChunkNetwork", bg.TREE_ID)
        _show_tree(context, tree)
        _open_stage(new_network_stage(), tree=tree)
        self.report({"INFO"}, "new chunk network")
        return {"FINISHED"}


class USDCHUNKS_OT_save_network(bpy.types.Operator):
    """Write node positions back and save the .usda; asks where for a new network."""

    bl_idname = "usd_chunks.save_network"
    bl_label = "Save Chunk Network"
    bl_options = {"REGISTER"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.usda;*.usd;*.usdc", options={"HIDDEN"})
    #: Save As: ask for a path even when the network already has one.
    save_as: bpy.props.BoolProperty(default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        view = get_view()
        tree = view.tree if view is not None else None
        path = tree.chunk_network_path if tree is not None else ""
        if path and not self.save_as:
            self.filepath = path
            return self.execute(context)
        self.filepath = path or f"{tree.name if tree is not None else 'network'}.usda"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        view = get_view()
        if view is None:
            self.report({"WARNING"}, "no chunk network open")
            return {"CANCELLED"}
        if not self.filepath:
            self.report({"WARNING"}, "no file to save to")
            return {"CANCELLED"}
        if not os.path.splitext(self.filepath)[1]:
            self.filepath += ".usda"
        write_network(view, self.filepath)
        tree = view.tree
        if tree is not None:
            tree.chunk_network_path = self.filepath
            tree.chunk_network_embedded = ""  # the file is the copy now
        self.report({"INFO"}, f"saved {self.filepath}")
        return {"FINISHED"}


class USDCHUNKS_OT_auto_layout(bpy.types.Operator):
    """Lay out the selection's connected subgraph, or everything."""

    bl_idname = "usd_chunks.auto_layout"
    bl_label = "Auto Layout"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        view = get_view()
        if view is None:
            return {"CANCELLED"}
        view.pull_selection_from_tree()
        view.auto_layout()
        return {"FINISHED"}


class USDCHUNKS_OT_set_view_slot(bpy.types.Operator):
    """Switch which view drives execution, and recook for it."""

    bl_idname = "usd_chunks.set_view_slot"
    bl_label = "Set Active View"
    bl_options = {"REGISTER", "UNDO"}

    slot: bpy.props.IntProperty(default=1, min=0, max=2)

    def execute(self, context):
        view = get_view()
        if view is None:
            return {"CANCELLED"}
        view.set_active_slot(self.slot)
        self.report({"INFO"}, view.status)
        return {"FINISHED"}


class USDCHUNKS_OT_dive_into(bpy.types.Operator):
    """Open the selected chunkgroup as its own graph."""

    bl_idname = "usd_chunks.dive_into"
    bl_label = "Dive Into Group"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        # Only in our own editor. This shares Tab with Blender's node.group_edit, and a
        # poll that said yes in a shader editor would hijack entering a shader group.
        return getattr(context.space_data, "tree_type", "") == bg.TREE_ID

    def execute(self, context):
        view = get_view()
        if view is None:
            return {"CANCELLED"}
        view.pull_selection_from_tree()
        targets = [p for p in view.selected_nodes if view.can_dive(p)]
        if not targets:
            # Nothing selected that can be dived, but the graph is one group and nothing
            # else: that group is what is meant. It is the shape every saved network has
            # at its top, and "select it first" is a step nobody should have to learn.
            groups = [p for p in view.prims if view.can_dive(p)]
            if len(groups) == 1 and len(view.prims) == 1:
                targets = groups
        if not targets:
            self.report({"WARNING"}, "select a chunkgroup to dive into")
            return {"CANCELLED"}
        view.dive_into(targets[0])
        self.report({"INFO"}, view.status)
        return {"FINISHED"}


class USDCHUNKS_OT_publish_network(bpy.types.Operator):
    """Write the selected chunkgroup to its own file and host it from there.

    Afterwards the group's chunks compose in from that file and anything this graph does
    to them is an override -- the sequence half of the sequence/shot split. Localise is
    the way back.
    """

    bl_idname = "usd_chunks.publish_network"
    bl_label = "Publish Network"
    bl_options = {"REGISTER"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.usda;*.usd;*.usdc", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", "") == bg.TREE_ID

    def invoke(self, context, event):
        chunk = _selected_chunk(predicate=chunk_stage.is_chunkgroup)
        if chunk is None:
            self.report({"WARNING"}, "select a chunkgroup to publish")
            return {"CANCELLED"}
        self.filepath = f"{chunk.name}.usda"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        chunk = _selected_chunk(predicate=chunk_stage.is_chunkgroup)
        if chunk is None or not self.filepath:
            return {"CANCELLED"}
        chunk_stage.publish_network(chunk, self.filepath)
        view = get_view()
        if view is not None:
            view.reload()
        self.report({"INFO"}, f"published {self.filepath}")
        return {"FINISHED"}


class USDCHUNKS_OT_localise_network(bpy.types.Operator):
    """Take a hosted network into this file as an ordinary chunkgroup.

    The file's chunks, with whatever this graph overrode on top, become local defs and the
    arc is dropped -- "Allow Editing of Contents". The inverse of Publish.
    """

    bl_idname = "usd_chunks.localise_network"
    bl_label = "Localise Network"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", "") == bg.TREE_ID

    def execute(self, context):
        chunk = _selected_chunk(predicate=chunk_stage.is_localisable)
        if chunk is None:
            self.report({"WARNING"}, "select a hosted network to localise")
            return {"CANCELLED"}
        name = chunk.name
        chunk_stage.localise_network(chunk)
        view = get_view()
        if view is not None:
            view.reload()
        self.report({"INFO"}, f"localised {name}")
        return {"FINISHED"}


def _selected_chunk(predicate):
    """The one selected chunk the predicate accepts, or -- when nothing selected fits --
    the graph's single candidate. Same "do not make me select the only thing there"
    courtesy dive_into already extends, and both verbs act on exactly one chunk.
    """
    view = get_view()
    if view is None:
        return None
    view.pull_selection_from_tree()
    chosen = [c for p, c in view.chunks.items()
              if p in view.selected_nodes and predicate(c)]
    if not chosen:
        candidates = [c for p, c in view.chunks.items() if p in view.prims and predicate(c)]
        if len(candidates) == 1:
            chosen = candidates
    return chosen[0] if len(chosen) == 1 else None


class USDCHUNKS_OT_dive_out(bpy.types.Operator):
    """Back up to the graph that holds this one."""

    bl_idname = "usd_chunks.dive_out"
    bl_label = "Dive Out"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", "") == bg.TREE_ID

    def execute(self, context):
        view = get_view()
        if view is None:
            return {"CANCELLED"}
        view.dive_out()
        self.report({"INFO"}, view.status)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# The editing verbs, shadowing Blender's own node operators inside this tree type
#
# X, Shift+D, Ctrl+C/X/V, M and H all have stock node-editor operators, and every one of them
# edits the bpy tree and nothing else. The tree is a view, so that is the wrong half: a
# deleted node came back on the next reload, a duplicate copied `chunk_path` and so edited
# and wired the ORIGINAL prim, M set node.mute and left the chunk enabled. These take the
# same keys (add-on keymaps win over the defaults) and poll only in a chunk tree, so in a
# shader editor each key still means what it always did.
# ---------------------------------------------------------------------------

class _ChunkTreeOperator(bpy.types.Operator):
    """Shared shape: only in a chunk tree, only with a view, selection read from the
    editor first because Blender owns clicking and box-select.
    """

    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", "") == bg.TREE_ID

    def run(self, context, view) -> None:
        raise NotImplementedError

    def execute(self, context):
        view = get_view()
        if view is None:
            self.report({"WARNING"}, "no chunk network open")
            return {"CANCELLED"}
        view.pull_selection_from_tree()
        self.run(context, view)
        if view.status:
            self.report({"INFO"}, view.status)
        return {"FINISHED"}


class USDCHUNKS_OT_delete(_ChunkTreeOperator):
    """Delete the selected chunks, healing the exec chain across the gap."""

    bl_idname = "usd_chunks.delete"
    bl_label = "Delete Chunks"

    def run(self, context, view):
        view.delete_selected()


class USDCHUNKS_OT_duplicate(_ChunkTreeOperator):
    """Duplicate the selected chunks as new chunks, offset from the originals."""

    bl_idname = "usd_chunks.duplicate"
    bl_label = "Duplicate Chunks"

    def run(self, context, view):
        view.duplicate_selection()


class USDCHUNKS_OT_copy(_ChunkTreeOperator):
    """Copy the selection to the chunk clipboard -- the one every frontend shares."""

    bl_idname = "usd_chunks.copy"
    bl_options = {"REGISTER"}  # copying changes nothing to undo
    bl_label = "Copy Chunks"

    def run(self, context, view):
        view.copy_selection()


class USDCHUNKS_OT_cut(_ChunkTreeOperator):
    """Cut the selection to the chunk clipboard."""

    bl_idname = "usd_chunks.cut"
    bl_label = "Cut Chunks"

    def run(self, context, view):
        view.cut_selection()


class USDCHUNKS_OT_paste(_ChunkTreeOperator):
    """Paste the chunk clipboard at the cursor."""

    bl_idname = "usd_chunks.paste"
    bl_label = "Paste Chunks"

    def run(self, context, view):
        cursor = getattr(context.space_data, "cursor_location", None)
        x, y = (cursor[0] / bg._SCALE, cursor[1] / bg._SCALE) if cursor else (0.0, 0.0)
        view.paste_at(x, y)


class USDCHUNKS_OT_toggle_disable(_ChunkTreeOperator):
    """Disable or re-enable the selected chunks. A disabled chunk stays, muted."""

    bl_idname = "usd_chunks.toggle_disable"
    bl_label = "Toggle Disable"

    def run(self, context, view):
        view.toggle_disable_selected()


class USDCHUNKS_OT_collapse(_ChunkTreeOperator):
    """Collapse the selection, or the whole graph; expand again once all are collapsed."""

    bl_idname = "usd_chunks.collapse"
    bl_label = "Collapse Chunks"

    def run(self, context, view):
        view.collapse_selected_or_all()


class USDCHUNKS_OT_group(_ChunkTreeOperator):
    """Put the selection in a chunkgroup that takes its place in the exec chain."""

    bl_idname = "usd_chunks.group"
    bl_label = "Group Into Chunkgroup"

    def run(self, context, view):
        view.group_selection()


class USDCHUNKS_OT_create_view(_ChunkTreeOperator):
    """Wire a view to the selected chunk (or the end of the chain) and make it active."""

    bl_idname = "usd_chunks.create_view"
    bl_label = "Create View"

    def run(self, context, view):
        view.create_view_node()


class USDCHUNKS_PT_panel(bpy.types.Panel):
    """The node editor sidebar."""

    bl_idname = "USDCHUNKS_PT_panel"
    bl_label = "USDChunks"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "USDChunks"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return getattr(space, "tree_type", "") == bg.TREE_ID

    def draw(self, context):
        layout = self.layout
        dependencies.draw_progress(layout)
        dependencies.draw_missing(layout, context)
        tree = _active_tree()
        view = get_view()
        layout.operator(USDCHUNKS_OT_new_network.bl_idname, icon="ADD")
        layout.operator(USDCHUNKS_OT_open_network.bl_idname, icon="FILE_FOLDER")
        row = layout.row(align=True)
        row.operator(USDCHUNKS_OT_save_network.bl_idname, icon="FILE_TICK")
        row.operator(USDCHUNKS_OT_save_network.bl_idname, text="",
                     icon="FILE_NEW").save_as = True
        if tree is not None:
            # Where the network lives, so "is my work on disk?" never needs a guess.
            if not tree.chunk_network_path:
                layout.label(text="No file yet -- kept in the .blend", icon="ERROR")
            elif view is not None and view.stage.GetRootLayer().dirty:
                layout.label(text=f"{bpy.path.basename(tree.chunk_network_path)} -- unsaved edits",
                             icon="ERROR")
            else:
                layout.label(text=bpy.path.basename(tree.chunk_network_path), icon="FILE")
        missing = getattr(view, "missing_types", None) if view is not None else None
        if missing:
            # Kept on screen, not only reported once: a report scrolls away, and the
            # network keeps cooking without these chunks until the user fixes the path.
            box = layout.box()
            box.alert = True
            count = sum(len(paths) for paths in missing.values())
            box.label(text=f"{count} chunk(s) have no module", icon="ERROR")
            for name in sorted(missing):
                box.label(text=f"{name}  ({len(missing[name])})")
            box.label(text="Add their folder to USD_CHUNK_DIRS")
        layout.separator()
        layout.operator(USDCHUNKS_OT_auto_layout.bl_idname, icon="GRAPH")
        row = layout.row(align=True)
        row.operator(USDCHUNKS_OT_group.bl_idname, text="Group", icon="GROUP")
        row.operator(USDCHUNKS_OT_create_view.bl_idname, text="View", icon="HIDE_OFF")

        box = layout.box()
        # Where you are. A network's top level is usually one group and nothing else, so
        # without this line the editor looks empty-but-for-a-node and gives no hint that
        # there is anywhere to go.
        box.label(text=(view.context.rsplit("/", 1)[-1] if view else "-"), icon="NODETREE")
        row = box.row(align=True)
        row.operator(USDCHUNKS_OT_dive_into.bl_idname, text="Dive In", icon="FULLSCREEN_ENTER")
        row.operator(USDCHUNKS_OT_dive_out.bl_idname, text="Dive Out", icon="FULLSCREEN_EXIT")
        row = box.row(align=True)
        row.operator(USDCHUNKS_OT_publish_network.bl_idname, text="Publish", icon="EXPORT")
        row.operator(USDCHUNKS_OT_localise_network.bl_idname, text="Localise", icon="IMPORT")

        box = layout.box()
        box.label(text="Active view")
        row = box.row(align=True)
        active = view.active_slot() if view else 0
        for slot in (0,) + chunk_graph.VIEW_SLOTS:
            op = row.operator(USDCHUNKS_OT_set_view_slot.bl_idname,
                              text=slot_label(slot), depress=(slot == active))
            op.slot = slot
        # The keys, so nobody has to find them in the keymap editor: they mirror the Qt
        # and Flet graphs (V, 1-3, L, Ctrl+G) where Blender had no habit to keep, and
        # Blender's own (X, Shift+D, Ctrl+C/X/V, M, Tab) where it did.
        box.label(text="V view  1 output  2-3 views  L layout  Ctrl+G group", icon="EVENT_V")

        if tree is not None and tree.chunk_network_path:
            layout.label(text=tree.chunk_network_path.rsplit("\\", 1)[-1], icon="FILE")
        if view is not None and view.status:
            layout.label(text=view.status)


def _register_delegates(_prefs=None, _context=None) -> list[str]:
    """Make the delegates in USD_CHUNKS_HYDRA_DELEGATES and the preference available.

    Also the preference's update callback, so a folder typed in takes effect at once: the
    Renderer menu asks USD for its delegates every time it opens.
    """
    folders = delegates.split_folders(os.environ.get(delegates.ENV_VAR, ""))
    addon_entry = bpy.context.preferences.addons.get(__package__)
    if addon_entry is not None:
        folders += delegates.split_folders(addon_entry.preferences.hydra_delegate_folders)
    return delegates.register_folders(folders)


class USDCHUNKS_AP_preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    hydra_delegate_folders: bpy.props.StringProperty(
        name="Hydra Delegate Folders",
        description=(
            "Render delegates for the live view, built against Blender's USD: each folder "
            "holds plugInfo.json or a resources folder with one. Separate several with ';'"),
        default="",
        update=_register_delegates,
    )

    def draw(self, _context):
        self.layout.prop(self, "hydra_delegate_folders")
        self.layout.label(text="Pick one in the 3D viewport: N panel > USDChunks > Renderer",
                          icon="INFO")


_CLASSES = (
    USDCHUNKS_AP_preferences,
    USDCHUNKS_OT_open_network,
    USDCHUNKS_OT_new_network,
    USDCHUNKS_OT_save_network,
    USDCHUNKS_OT_auto_layout,
    USDCHUNKS_OT_set_view_slot,
    USDCHUNKS_OT_dive_into,
    USDCHUNKS_OT_dive_out,
    USDCHUNKS_OT_publish_network,
    USDCHUNKS_OT_localise_network,
    USDCHUNKS_OT_delete,
    USDCHUNKS_OT_duplicate,
    USDCHUNKS_OT_copy,
    USDCHUNKS_OT_cut,
    USDCHUNKS_OT_paste,
    USDCHUNKS_OT_toggle_disable,
    USDCHUNKS_OT_collapse,
    USDCHUNKS_OT_group,
    USDCHUNKS_OT_create_view,
    USDCHUNKS_PT_panel,
) + _USD_HOOKS

#: (keymap, keymap_item) pairs, so unregister removes exactly what register added.
_keymaps: list = []

_NUMBER_KEYS = {1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR"}


def slot_number(slot: int) -> int:
    """The number key that picks a slot: 1 for the Output (slot 0), then the views."""
    return chunk_graph.OUTPUT_NUMBER if slot == 0 else chunk_graph.view_number(slot)


def slot_label(slot: int) -> str:
    """How a slot reads on its button, the way the Qt graph names it."""
    return f"Output [{slot_number(slot)}]" if slot == 0 else f"View [{slot_number(slot)}]"


#: (operator, key, modifiers, operator properties). Blender's own node keys keep their
#: meaning -- Tab/Ctrl+Tab for groups, X and Delete, Shift+D, Ctrl+C/X/V, M, H -- routed to
#: the chunk verbs; the graph keys the Qt and Flet frontends share (V, 0-3, L, Ctrl+G)
#: fill in where Blender had no habit to keep.
_KEYS = (
    (USDCHUNKS_OT_dive_into, "TAB", {}, {}),
    (USDCHUNKS_OT_dive_out, "TAB", {"ctrl": True}, {}),
    (USDCHUNKS_OT_delete, "X", {}, {}),
    (USDCHUNKS_OT_delete, "DEL", {}, {}),
    (USDCHUNKS_OT_duplicate, "D", {"shift": True}, {}),
    (USDCHUNKS_OT_copy, "C", {"ctrl": True}, {}),
    (USDCHUNKS_OT_cut, "X", {"ctrl": True}, {}),
    (USDCHUNKS_OT_paste, "V", {"ctrl": True}, {}),
    (USDCHUNKS_OT_toggle_disable, "M", {}, {}),
    (USDCHUNKS_OT_collapse, "H", {}, {}),
    (USDCHUNKS_OT_group, "G", {"ctrl": True}, {}),
    (USDCHUNKS_OT_create_view, "V", {}, {}),
    (USDCHUNKS_OT_auto_layout, "L", {}, {}),
) + tuple(
    # 1 is the Output and 2, 3 the views, as in Qt. From the engine's numbering, so the
    # two graphs cannot drift apart again.
    (USDCHUNKS_OT_set_view_slot, _NUMBER_KEYS[slot_number(slot)], {}, {"slot": slot})
    for slot in (0,) + chunk_graph.VIEW_SLOTS
)


def _register_keymap() -> None:
    """Bind _KEYS in the add-on keyconfig, which Blender consults before its defaults --
    so inside a chunk tree these win over node.delete, node.duplicate_move and the rest,
    and everywhere else those operators' poll fails and the key falls through to what it
    always did.
    """
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:
        return  # background mode: no key configs, nothing to bind to
    km = kc.keymaps.new(name="Node Editor", space_type="NODE_EDITOR")
    for op, key, mods, props in _KEYS:
        kmi = km.keymap_items.new(op.bl_idname, key, "PRESS", **mods)
        for name, value in props.items():
            setattr(kmi.properties, name, value)
        _keymaps.append((km, kmi))


@bpy.app.handlers.persistent
def _on_undo(scene, *_args) -> None:
    """Bring the network back to the undo step Blender just restored.

    Blender's undo restores the .blend -- the node tree, every RNA value on every node --
    and knows nothing about the stage. undo.py records each change to the network's chunk
    prims and keeps the id of the step the network is at on the tree, so the step Blender
    restored says where the network was: walk the chunk prims back (or forward) to it, then
    rebuild the tree and cook the graph on screen. Without
    this, Ctrl+Z only rebuilt the tree from USD, so a param edit, a new node or a delete
    was never undone.

    A memfile undo can also recreate the NodeTree datablock itself; `BlenderGraphView.tree`
    already looks the tree up by name for exactly that reason, so a reload here lands in
    the recreated one.
    """
    view = get_view()
    if view is None:
        return
    try:
        restored = undo.restore(view.tree, view.stage)
        if restored:
            # A chunk whose prim came back (undoing a delete) is a new chunk object.
            view.chunks = {chunk_stage.get_chunk_prim_path(c): c
                           for c in view.scene.get_chunks()}
        view.reload()
        if restored:
            _cook_shown_graph(view)
    except Exception:  # pragma: no cover - an undo past the network's open
        logger.exception("could not rebuild the chunk tree after undo")


def _cook_shown_graph(view) -> None:
    """Cook for the graph on screen, with its active view, as Qt does (recook_focus)."""
    context_chunk = bg.chunk_graph.context_chunk(view.scene, view.chunks, view.context)
    slot = view.active_slot()
    view_chunk = (chunk_stage.find_view_chunk_for_slot(
        slot, view.stage, view.scene, context_chunk) if slot else None)
    chunk_stage.recook_focus(view.stage, view.scene, context_chunk, view_chunk)


def _unregister_keymap() -> None:
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()


def register() -> None:
    configure_logging()
    bg.register()
    for cls in dependencies.CLASSES + _CLASSES:
        bg._register_once(cls)
    # On the tree, not in a global: two trees can edit two networks, and the path has to
    # survive a .blend save so reopening the file knows what it was pointed at.
    bpy.types.NodeTree.chunk_network_path = bpy.props.StringProperty(
        name="Chunk Network", subtype="FILE_PATH", default="")
    # Also per-tree: whether _open_stage has ever run for it. See _poll_new_trees.
    bpy.types.NodeTree.chunk_network_initialized = bpy.props.BoolProperty(default=False)
    # A network with no file yet, as .usda text, written on .blend save (_on_save_pre).
    bpy.types.NodeTree.chunk_network_embedded = bpy.props.StringProperty(
        default="", options={"HIDDEN"})
    if not bpy.app.timers.is_registered(_poll_new_trees):
        # Short first interval: this is what stands between clicking "+ New" and the Add
        # menu / registry actually being ready, so 0.5s read as "Shift+A does nothing,
        # try again a moment later."
        bpy.app.timers.register(_poll_new_trees, first_interval=0.05, persistent=True)
    _register_keymap()
    if _on_save_pre not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_on_save_pre)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if not bpy.app.timers.is_registered(_run_due_calls):
        bpy.app.timers.register(_run_due_calls, first_interval=_LATER_INTERVAL, persistent=True)
    if not bpy.app.timers.is_registered(_sync_host_sources):
        bpy.app.timers.register(_sync_host_sources, first_interval=_SOURCES_INTERVAL,
                                persistent=True)
    if _on_undo not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(_on_undo)
    if _on_undo not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(_on_undo)
    # Before the viewport: its Renderer menu lists whatever delegates USD knows by then.
    try:
        _register_delegates()
    except Exception:  # a bad folder must not take the add-on down with it
        logger.exception("could not register Hydra delegates")
    viewport.register()
    gizmos.register()
    brush.register()
    camera.register()


def unregister() -> None:
    undo.untrack()
    camera.unregister()
    brush.unregister()
    gizmos.unregister()
    viewport.unregister()
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_undo in handlers:
            handlers.remove(_on_undo)
    if _on_save_pre in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_on_save_pre)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if bpy.app.timers.is_registered(_notify_host_changed):
        bpy.app.timers.unregister(_notify_host_changed)
    if bpy.app.timers.is_registered(_sync_host_sources):
        bpy.app.timers.unregister(_sync_host_sources)
    if bpy.app.timers.is_registered(_run_due_calls):
        bpy.app.timers.unregister(_run_due_calls)
    _unregister_keymap()
    if bpy.app.timers.is_registered(_poll_new_trees):
        bpy.app.timers.unregister(_poll_new_trees)
    if bpy.app.timers.is_registered(_open_pending_imports):
        bpy.app.timers.unregister(_open_pending_imports)
    _pending_imports.clear()
    dependencies.unregister()
    for cls in reversed(dependencies.CLASSES + _CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # pragma: no cover - already gone
            pass
    bg.unregister()
    if hasattr(bpy.types.NodeTree, "chunk_network_path"):
        del bpy.types.NodeTree.chunk_network_path
    if hasattr(bpy.types.NodeTree, "chunk_network_initialized"):
        del bpy.types.NodeTree.chunk_network_initialized
    if hasattr(bpy.types.NodeTree, "chunk_network_embedded"):
        del bpy.types.NodeTree.chunk_network_embedded
